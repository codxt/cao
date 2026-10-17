"""Focused tests for the product-facing ``./cao`` command line.

The doctor/readiness logic is covered in ``test_bootstrap``; here the concern is
the CLI surface: structured output, exit codes, the honest ``NOT_STARTED``
service answer, fail-closed behavior on corrupt state, and explicit
``UNAVAILABLE`` for commands this build does not own.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cao import (
    product_cli,
    product_config,
    product_daemon,
    product_driver,
    product_state,
    registry,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def git_repo(root: Path, name: str = "repo") -> Path:
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
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "init")
    return repo


class OccupancyDriver:
    """A fake runtime whose occupancy the WRITE claim path can read."""

    def __init__(self) -> None:
        self.readable = True
        self.wait_state = "working"
        self.calls: list[str] = []
        self._workspaces: dict[str, dict] = {}
        self._panes: dict[str, dict] = {}

    def workspace_list(self):
        self.calls.append("workspace_list")
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
        self.calls.append("pane_list")
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
        if not self.readable:
            raise product_driver.DriverTransportError("runtime unavailable")
        pane = self._panes.get(pane_id)
        if pane is None:
            raise product_driver.DriverNotFound("no such pane")
        return dict(pane)

    def workspace_create(self, label, cwd=None):
        self.calls.append("workspace_create")
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
        return "{}"

    def agent_start(self, name, kind, pane_id, *, argv=(), timeout_ms=None):
        self.calls.append("agent_start")
        pane = self._panes.get(pane_id)
        if pane is not None:
            pane["agent"] = name
            pane["agent_status"] = "working"
        return {"agent_pane_id": pane_id, "agent_workspace_id": None}

    def agent_prompt(self, target, text, *, timeout_ms=None, wait=False):
        self.calls.append("agent_prompt")
        return {"agent_pane_id": "w1:p1", "agent_workspace_id": None}

    def agent_read(self, target, *, lines=200, source="recent"):
        return "output"

    def agent_get(self, target):
        raise product_driver.DriverNotFound("not present")

    def agent_wait(self, target, *, until=(), timeout_ms=None):
        return {"agent": {"agent_status": self.wait_state}}


def run_cli(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = product_cli.main(argv)
    return code, buffer.getvalue()


class CliCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = {
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
            # ``registry_db_path`` reads ``CAO_REGISTRY_DB`` first, so an
            # inherited value would beat both XDG homes and send these CLI
            # tests' synthetic claims into a shared live ledger. Force the
            # fixture's own private ledger explicitly; ``clear=False`` alone
            # does not override a variable the host already exports.
            "CAO_REGISTRY_DB": str(self.root / "registry.sqlite3"),
        }
        patcher = mock.patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "CAO_REGISTRY_DB"):
            self.addCleanup(os.environ.pop, key, None)


class FixtureRegistryIsolationTests(CliCase):
    """The CLI fixture must own its ledger even under a hostile inherited env.

    ``product_config.registry_db_path`` reads ``CAO_REGISTRY_DB`` before any XDG
    home, so a host that exports its shared ledger must not be able to redirect
    a CLI test into it. This fails if the fixture stops forcing a private
    ledger.
    """

    def setUp(self) -> None:
        # Simulate an inherited live selection BEFORE the fixture runs.
        inherited = tempfile.TemporaryDirectory()
        self.addCleanup(inherited.cleanup)
        self.inherited_db = Path(inherited.name) / "cao-work-registry" / "registry.sqlite3"
        previous = os.environ.get("CAO_REGISTRY_DB")
        self.addCleanup(self._restore_registry_db, previous)
        os.environ["CAO_REGISTRY_DB"] = str(self.inherited_db)
        super().setUp()

    @staticmethod
    def _restore_registry_db(previous: str | None) -> None:
        if previous is None:
            os.environ.pop("CAO_REGISTRY_DB", None)
        else:
            os.environ["CAO_REGISTRY_DB"] = previous

    def test_inherited_ledger_is_replaced_by_a_private_local_ledger(self) -> None:
        selected = Path(product_config.registry_db_path(os.environ))
        self.assertTrue(selected.is_relative_to(self.root), selected)
        self.assertNotEqual(selected, self.inherited_db)
        # Selecting the fixture ledger must not have touched the inherited one.
        self.assertFalse(self.inherited_db.exists())


class HelpTests(CliCase):
    def test_help_exits_zero_and_lists_commands(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            with self.assertRaises(SystemExit) as ctx:
                product_cli.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        text = buffer.getvalue()
        for command in ("doctor", "status", "run", "start", "stop", "logs",
                        "request"):
            self.assertIn(command, text)


class RequestEntrypointTests(CliCase):
    def test_request_group_is_discoverable_through_the_product_cli(self) -> None:
        code, output = run_cli(["request", "--item-id", "p1", "--text", "fix it"])
        self.assertEqual(code, product_cli.EXIT_OK)
        payload = json.loads(output)
        self.assertEqual(payload["schema"], "cao.request.v1")
        # "fix it" clearly expresses implementation intent, so the single
        # entrypoint surfaces it as an execution candidate, not backlog.
        self.assertEqual(payload["decision"], "IMPLEMENT")
        self.assertEqual(payload["route"], "EXECUTION")
        self.assertEqual(payload["status"], "PROPOSED")
        self.assertFalse(payload["dispatch"])
        self.assertFalse(payload["execution"]["started"])


class DoctorCliTests(CliCase):
    def test_absent_config_is_not_ready(self) -> None:
        code, output = run_cli(["doctor"])
        self.assertEqual(code, product_cli.EXIT_UNAVAILABLE)
        payload = json.loads(output)
        self.assertEqual(payload["schema"], "cao.product.doctor.v0")
        self.assertEqual(payload["status"], "NOT_READY")


class StatusCliTests(CliCase):
    def test_absent_state_does_not_invent_a_service(self) -> None:
        code, output = run_cli(["status"])
        self.assertEqual(code, product_cli.EXIT_OK)
        payload = json.loads(output)
        self.assertEqual(payload["schema"], "cao.product.status.v0")
        self.assertEqual(payload["status"], "NOT_STARTED")

    def test_reports_a_persisted_record(self) -> None:
        product_state.write_service(
            {
                "schema": product_state.SERVICE_SCHEMA,
                "desired": "running",
                "observed": "running",
                "updated_at": "2026-01-01T00:00:00Z",
            }
        )
        code, output = run_cli(["status"])
        self.assertEqual(code, product_cli.EXIT_OK)
        self.assertEqual(json.loads(output)["status"], "UNKNOWN")

    def test_stale_service_does_not_claim_running(self) -> None:
        product_state.write_service({
            "schema": product_state.SERVICE_SCHEMA,
            "desired": "running", "observed": "running",
            "updated_at": "2026-01-01T00:00:00Z",
        })
        # No actual daemon record or live instance exists.
        code, output = run_cli(["status"])
        self.assertEqual(code, product_cli.EXIT_OK)
        result = json.loads(output)
        self.assertNotEqual(result["status"], "RUNNING")
        self.assertNotEqual(result["observed"], "running")

    def test_corrupt_state_fails_closed(self) -> None:
        path = product_state.service_record_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text("{ not json", encoding="utf-8")
        code, output = run_cli(["status"])
        self.assertEqual(code, product_cli.EXIT_UNAVAILABLE)
        payload = json.loads(output)
        self.assertEqual(payload["schema"], "cao.product.unavailable.v0")
        self.assertEqual(payload["error"]["code"], "SERVICE_STATE_UNREADABLE")


class UnavailableCommandTests(CliCase):
    def test_owned_elsewhere_commands_are_explicitly_unavailable(self) -> None:
        for command in ("run", "start", "stop", "logs"):
            code, output = run_cli([command])
            self.assertEqual(code, product_cli.EXIT_UNAVAILABLE)
            payload = json.loads(output)
            self.assertEqual(payload["schema"], "cao.product.unavailable.v0")
            self.assertEqual(payload["status"], "UNAVAILABLE")
            self.assertEqual(payload["command"], command)
            self.assertEqual(payload["error"]["code"], "NOT_IMPLEMENTED")

    def test_run_accepts_task_arguments_before_reporting_unavailable(self) -> None:
        code, output = run_cli(["run", "--repo", "/tmp/project", "Review this"])
        self.assertEqual(code, product_cli.EXIT_UNAVAILABLE)
        self.assertEqual(json.loads(output)["command"], "run")


class WrapperTests(CliCase):
    def test_root_wrapper_dispatches_to_the_product_cli(self) -> None:
        wrapper = REPO_ROOT / "cao"
        self.assertTrue(os.access(wrapper, os.X_OK))
        result = subprocess.run(
            [str(wrapper), "status"],
            env=os.environ.copy(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["status"], "NOT_STARTED")


class WriteRunCliTests(CliCase):
    """The CLI WRITE path supplies a live occupancy observation to the gate.

    Before this, ``cao run --write`` reached the registry with no observation,
    so every real WRITE claim was refused as unverifiable even when the runtime
    was healthy. These tests fail on that behavior.
    """

    def _setup(self, driver: OccupancyDriver) -> Path:
        repo = git_repo(self.root)
        product_config.write_config(
            {
                "schema": product_config.SCHEMA,
                "provider": {"cli": "opencode", "model": "m"},
            }
        )
        exe = self.root / "runtime"
        exe.write_text("#!/bin/sh\n", encoding="utf-8")
        exe.chmod(0o755)
        product_state.write_managed(
            {"schema": product_state.MANAGED_SCHEMA, "path": str(exe)}
        )
        patcher = mock.patch.object(
            product_driver, "RuntimeDriver", lambda **_kwargs: driver
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return repo

    def test_write_task_claims_write_from_live_occupancy(self) -> None:
        driver = OccupancyDriver()
        repo = self._setup(driver)
        code, output = run_cli(["run", "--repo", str(repo), "--write", "a.txt", "edit"])
        self.assertEqual(code, product_cli.EXIT_OK, output)
        payload = json.loads(output)
        self.assertEqual(payload["mode"], "WRITE")
        self.assertEqual(payload["claim"]["mode"], "WRITE")
        self.assertIn("agent_prompt", driver.calls)
        conn = registry.connect(db=product_config.registry_db_path(os.environ))
        self.addCleanup(conn.close)
        rows = conn.execute("SELECT mode FROM claims").fetchall()
        self.assertEqual([row["mode"] for row in rows], ["WRITE"])

    def test_overlapping_write_is_refused_by_the_ledger(self) -> None:
        driver = OccupancyDriver()
        repo = self._setup(driver)
        first_code, first_out = run_cli(
            ["run", "--repo", str(repo), "--id", "w1", "--write", "a.txt", "edit"]
        )
        self.assertEqual(first_code, product_cli.EXIT_OK, first_out)
        second_code, second_out = run_cli(
            ["run", "--repo", str(repo), "--id", "w2", "--write", "a.txt", "edit"]
        )
        self.assertEqual(second_code, product_cli.EXIT_UNAVAILABLE)
        payload = json.loads(second_out)
        self.assertEqual(payload["status"], "refused")
        self.assertEqual(payload["refusal"]["status"], "CONFLICT")

    def test_write_refuses_when_occupancy_is_unreadable(self) -> None:
        driver = OccupancyDriver()
        driver.readable = False
        repo = self._setup(driver)
        code, output = run_cli(["run", "--repo", str(repo), "--write", "a.txt", "edit"])
        self.assertEqual(code, product_cli.EXIT_UNAVAILABLE)
        self.assertEqual(json.loads(output)["status"], "refused")
        self.assertNotIn("workspace_create", driver.calls)
        self.assertNotIn("agent_start", driver.calls)

    def test_write_refuses_when_an_occupant_cannot_be_placed(self) -> None:
        driver = OccupancyDriver()
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
        repo = self._setup(driver)
        code, output = run_cli(["run", "--repo", str(repo), "--write", "a.txt", "edit"])
        self.assertEqual(code, product_cli.EXIT_UNAVAILABLE)
        self.assertEqual(json.loads(output)["status"], "refused")

    def test_only_write_collects_occupancy(self) -> None:
        driver = OccupancyDriver()
        repo = self._setup(driver)
        spy = mock.Mock(wraps=product_daemon.collect_occupancy)
        patcher = mock.patch.object(
            product_cli.product_daemon, "collect_occupancy", spy
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        run_cli(["run", "--repo", str(repo), "--id", "r1", "review"])
        self.assertEqual(spy.call_count, 0)
        run_cli(["run", "--repo", str(repo), "--id", "w1", "--write", "a.txt", "edit"])
        self.assertEqual(spy.call_count, 1)


class StopStatusExitTests(unittest.TestCase):
    def test_stop_incomplete_is_nonzero_and_retains_json(self):
        config = product_config.validate_config({
            "schema": product_config.SCHEMA, "provider": {"cli": "opencode", "model": "x"},
        })
        payload = {"status": "running", "error": {"code": "TASK_PANE_CLOSE_FAILED"}}
        with mock.patch.object(product_cli, "_managed_executable", return_value=(None, "offline")), \
             mock.patch.object(product_cli.product_task, "stop_task", return_value=payload):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = product_cli._stop([], config)
        self.assertEqual(code, product_cli.EXIT_UNAVAILABLE)
        self.assertEqual(json.loads(out.getvalue()), payload)

    def test_stop_success_preserves_zero_exit(self):
        config = product_config.validate_config({
            "schema": product_config.SCHEMA, "provider": {"cli": "opencode", "model": "x"},
        })
        with mock.patch.object(product_cli, "_managed_executable", return_value=(None, "offline")), \
             mock.patch.object(product_cli.product_task, "stop_task", return_value={"status": "stopped"}):
            with contextlib.redirect_stdout(io.StringIO()):
                code = product_cli._stop([], config)
        self.assertEqual(code, product_cli.EXIT_OK)


if __name__ == "__main__":
    unittest.main()
