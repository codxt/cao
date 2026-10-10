"""Deterministic, fake-driver end-to-end proof of the CAO product flow.

This is the operating proof for the product layer. Everything here runs through
the real ``./cao`` launcher with the product CLI, an isolated XDG root under a
temp directory, and a *fake* managed runtime executable materialized in that
same temp directory. There is no live runtime, no network, and no touch of the
governed Work Registry ledger: the task layer attaches to a sandboxed ledger
under the isolated ``XDG_STATE_HOME``.

The fake runtime implements exactly the argv surface
:mod:`cao.product_driver` emits and answers with the JSON envelopes the driver
binds, persisting a tiny workspace/pane/agent model in a JSON state file so
successive invocations stay coherent. Its invocation log is the ground truth
for "exactly once": the suite counts ``workspace create``, ``agent start`` and
``agent prompt`` lines to prove a resume never duplicates work.

Pinned here:

* the full flow -- ``run`` -> ``status`` -> ``logs`` -> ``stop``;
* idempotent resume with no second prompt or start;
* an ambiguous prompt acknowledgement stays ``unknown`` (exit 3) and is never
  re-prompted;
* a WRITE task is refused without a scope, and a declared scope is recorded;
* a corrupt task record fails closed and is never treated as absent;
* the daemon reconciles idempotently (no duplicate effects on a double start)
  and re-drives a stale task without a second prompt or start;
* a missing provider or a missing managed binary fails closed (exit 3).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
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
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
LAUNCHER = REPO_ROOT / "cao"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}

#: The fake managed runtime. It is test-only and never shipped: it exists to
#: prove the product's argv surface and its exactly-once semantics hermetically.
FAKE_RUNTIME = r'''#!/usr/bin/env python3
"""A deterministic fake of the managed runtime CLI for hermetic tests."""
import json
import os
import sys

STATE = os.environ.get("FAKE_RUNTIME_STATE")
LOG = os.environ.get("FAKE_RUNTIME_LOG")
FAIL_PROMPT = os.environ.get("FAKE_RUNTIME_FAIL_PROMPT") == "1"


def _load():
    if STATE and os.path.isfile(STATE):
        with open(STATE, "r", encoding="utf-8") as handle:
            return json.load(handle)
    return {"workspaces": {}, "panes": {}, "agents": {}}


def _save(state):
    if not STATE:
        return
    temp = STATE + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    os.replace(temp, STATE)


def _log(argv):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(" ".join(argv) + "\n")


def _out(result_type, **fields):
    result = {"type": result_type}
    result.update(fields)
    sys.stdout.write(json.dumps({"id": "fake", "result": result}) + "\n")
    return 0


def _fail(code):
    sys.stderr.write(json.dumps({"error": {"code": code}}) + "\n")
    return 1


def _flag(argv, name, default=None):
    if name in argv:
        index = argv.index(name)
        if index + 1 < len(argv):
            return argv[index + 1]
    return default


def _main(argv):
    state = _load()
    if argv[:1] == ["workspace"]:
        action = argv[1] if len(argv) > 1 else ""
        if action == "list":
            workspaces = [
                {"workspace_id": w["workspace_id"], "label": w.get("label"),
                 "active_tab_id": w.get("active_tab_id")}
                for w in state["workspaces"].values()
            ]
            return _out("workspace_list", workspaces=workspaces)
        if action == "create":
            label = _flag(argv, "--label")
            cwd = _flag(argv, "--cwd") or ""
            number = len(state["workspaces"]) + 1
            workspace_id = "w%d" % number
            pane_id = workspace_id + ":p1"
            state["workspaces"][workspace_id] = {
                "workspace_id": workspace_id, "label": label,
                "active_tab_id": workspace_id + ":t1",
            }
            state["panes"][pane_id] = {
                "pane_id": pane_id, "workspace_id": workspace_id, "cwd": cwd,
                "agent": None, "agent_status": "unknown",
                "agent_session": {"kind": "id", "value": "session-" + workspace_id},
            }
            _save(state)
            return _out("workspace_created",
                        workspace={"workspace_id": workspace_id},
                        tab={"tab_id": workspace_id + ":t1"},
                        root_pane={"pane_id": pane_id})
        return _fail("workspace_not_found")
    if argv[:1] == ["pane"]:
        action = argv[1] if len(argv) > 1 else ""
        if action == "list":
            workspace_id = _flag(argv, "--workspace")
            panes = [
                {"pane_id": p["pane_id"], "workspace_id": p["workspace_id"],
                 "agent_status": p.get("agent_status")}
                for p in state["panes"].values()
                if p["workspace_id"] == workspace_id
            ]
            return _out("pane_list", panes=panes)
        if action == "get":
            pane = state["panes"].get(argv[2]) if len(argv) > 2 else None
            if pane is None:
                return _fail("pane_not_found")
            return _out("pane_info", pane=dict(pane))
        if action == "close":
            state["panes"].pop(argv[2], None)
            _save(state)
            sys.stdout.write("{}\n")
            return 0
        return _fail("pane_not_found")
    if argv[:1] == ["agent"]:
        action = argv[1] if len(argv) > 1 else ""
        if action == "start":
            name = argv[2]
            pane_id = _flag(argv, "--pane")
            pane = state["panes"].get(pane_id)
            if pane is None:
                return _fail("pane_not_found")
            pane["agent"] = name
            pane["agent_status"] = "working"
            state["agents"][name] = pane_id
            _save(state)
            return _out("agent_started",
                        agent={"pane_id": pane_id,
                               "workspace_id": pane["workspace_id"]})
        if action == "prompt":
            name = argv[2]
            if FAIL_PROMPT:
                return _fail("prompt_rejected")
            pane_id = state["agents"].get(name)
            if pane_id is None:
                return _fail("agent_not_found")
            pane = state["panes"].get(pane_id, {})
            return _out("agent_prompted",
                        agent={"pane_id": pane_id,
                               "workspace_id": pane.get("workspace_id")})
        if action == "read":
            sys.stdout.write("fake agent output for %s\n" % argv[2])
            return 0
        if action == "get":
            name = argv[2]
            if name in state["agents"]:
                return _out("agent_info",
                            agent={"name": name, "agent_status": "working"})
            return _fail("agent_not_found")
        if action == "wait":
            return _out("agent_wait", agent={"agent_status": "working"})
        return _fail("agent_not_found")
    return _fail("workspace_not_found")


if __name__ == "__main__":
    _log(sys.argv[1:])
    sys.exit(_main(sys.argv[1:]))
'''


class FakeClock:
    """A monotonic fake clock, injectable into the daemon and task layer."""

    def __init__(self) -> None:
        self._moment = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self._moment += timedelta(seconds=1)
        return self._moment


def git_repo(root: Path, name: str = "repo") -> Path:
    repo = root / name
    repo.mkdir()

    def run(*args: str) -> None:
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


class E2ECase(unittest.TestCase):
    """Shared sandbox: an isolated XDG root, a scratch repo, a fake runtime."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = git_repo(self.root, "project")
        self.state_home = self.root / "state"
        self.config_home = self.root / "config"
        self.config_path = self.config_home / "cao" / "config.json"
        self.db = str(self.root / "registry.sqlite3")
        self.state_file = self.root / "fake-state.json"
        self.log_file = self.root / "fake-invocations.txt"
        self.fake_exe = self.root / "fake-runtime"
        self.fake_exe.write_text(FAKE_RUNTIME, encoding="utf-8")
        self.fake_exe.chmod(0o755)

        self.xdg = {
            "XDG_STATE_HOME": str(self.state_home),
            "XDG_CONFIG_HOME": str(self.config_home),
        }
        product_config.write_config(
            {
                "schema": product_config.SCHEMA,
                "runtime_id": "runtime",
                "provider": {"cli": "opencode", "id": "opencode", "model": "m"},
                "task": {"agent_kind": "opencode"},
            },
            path=str(self.config_path),
        )
        self._write_managed(self.fake_exe)

        patcher = mock.patch.dict(
            os.environ,
            {
                **self.xdg,
                "FAKE_RUNTIME_STATE": str(self.state_file),
                "FAKE_RUNTIME_LOG": str(self.log_file),
            },
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in (
            *self.xdg,
            "FAKE_RUNTIME_STATE",
            "FAKE_RUNTIME_LOG",
            "FAKE_RUNTIME_FAIL_PROMPT",
        ):
            self.addCleanup(os.environ.pop, key, None)

    # -- helpers -----------------------------------------------------------

    def _write_managed(self, path: Path) -> None:
        product_state.write_managed(
            {"schema": product_state.MANAGED_SCHEMA, "path": str(path)}, self.xdg
        )

    def _cli_env(self) -> dict[str, str]:
        return {**os.environ, "PYTHONPATH": str(SRC_ROOT)}

    def _cli(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(LAUNCHER), *args],
            capture_output=True,
            text=True,
            env=env or self._cli_env(),
            timeout=60,
        )

    @staticmethod
    def _json(proc: subprocess.CompletedProcess) -> dict:
        return json.loads(proc.stdout)

    def _log_lines(self) -> list[str]:
        if not self.log_file.exists():
            return []
        return self.log_file.read_text(encoding="utf-8").splitlines()

    def _count(self, prefix: str) -> int:
        return sum(1 for line in self._log_lines() if line.startswith(prefix))

    def _drop_agents(self) -> None:
        """Simulate the agent exiting: remove it from the fake runtime state."""

        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        state["agents"] = {}
        for pane in state["panes"].values():
            pane["agent"] = None
            pane["agent_status"] = "unknown"
        self.state_file.write_text(json.dumps(state), encoding="utf-8")

    def _driver(self) -> product_driver.RuntimeDriver:
        return product_driver.RuntimeDriver(executable=str(self.fake_exe))

    def _config(self) -> product_config.ProductConfig:
        loaded = product_config.load_config(str(self.config_path))
        assert loaded is not None
        return loaded

    def _daemon(self, spawns: list[int]) -> product_daemon.Daemon:
        return product_daemon.Daemon(
            driver=self._driver(),
            config=self._config(),
            env=self.xdg,
            registry_db=self.db,
            clock=FakeClock(),
            sleep=lambda _seconds: None,
            spawn=lambda: (spawns.append(1), 4321)[1],
            alive=lambda pid: pid == 4321,
            terminate=lambda _pid: None,
            max_ticks=None,
        )


class FullFlowTests(E2ECase):
    def test_run_status_logs_stop_full_flow(self) -> None:
        run = self._cli("run", "--repo", str(self.repo), "--id", "tfull01", "review prompt")
        self.assertEqual(run.returncode, 0, run.stderr)
        payload = self._json(run)
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["prompt_state"], "accepted")
        self.assertEqual(payload["claim"]["mode"], "READ")
        self.assertEqual(payload["mode"], "READ")
        self.assertTrue(payload["workspace"]["label"].startswith("cao-review-"))

        status = self._cli("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        status_payload = self._json(status)
        self.assertEqual(status_payload["status"], "RUNNING")
        self.assertEqual(status_payload["task"]["task_id"], "tfull01")

        logs = self._cli("logs", "--id", "tfull01")
        self.assertEqual(logs.returncode, 0, logs.stderr)
        logs_payload = self._json(logs)
        self.assertTrue(logs_payload["ok"])
        self.assertIn("fake agent output", logs_payload["output"])

        stop = self._cli("stop", "--id", "tfull01")
        self.assertEqual(stop.returncode, 0, stop.stderr)
        stop_payload = self._json(stop)
        self.assertEqual(stop_payload["status"], "stopped")
        self.assertEqual(stop_payload["claim"]["outcome"], "abandon")
        self.assertEqual(stop_payload["pane_closed"], payload["pane_id"])

    def test_rerun_same_id_resumes_without_a_second_prompt_or_start(self) -> None:
        first = self._cli("run", "--repo", str(self.repo), "--id", "tresume01", "review prompt")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self._count("workspace create"), 1)
        self.assertEqual(self._count("agent start"), 1)
        self.assertEqual(self._count("agent prompt"), 1)

        second = self._cli("run", "--repo", str(self.repo), "--id", "tresume01", "review prompt")
        self.assertEqual(second.returncode, 0, second.stderr)
        second_payload = self._json(second)
        self.assertEqual(second_payload["task_id"], self._json(first)["task_id"])
        self.assertEqual(second_payload["status"], "running")
        # Exactly once: the resume reuses the workspace, agent and prompt.
        self.assertEqual(self._count("workspace create"), 1)
        self.assertEqual(self._count("agent start"), 1)
        self.assertEqual(self._count("agent prompt"), 1)


class AmbiguousPromptTests(E2ECase):
    def test_ambiguous_ack_is_unknown_exit_3_and_never_reprompted(self) -> None:
        env = self._cli_env()
        env["FAKE_RUNTIME_FAIL_PROMPT"] = "1"
        first = self._cli(
            "run", "--repo", str(self.repo), "--id", "tamb01", "review prompt", env=env
        )
        self.assertEqual(first.returncode, 3, first.stderr)
        first_payload = self._json(first)
        self.assertEqual(first_payload["status"], "unknown")
        self.assertEqual(first_payload["prompt_state"], "ambiguous")
        self.assertEqual(self._count("agent prompt"), 1)

        # A later run with a healthy runtime must not re-submit the prompt.
        second = self._cli("run", "--repo", str(self.repo), "--id", "tamb01", "review prompt")
        self.assertEqual(second.returncode, 3, second.stderr)
        second_payload = self._json(second)
        self.assertEqual(second_payload["status"], "unknown")
        self.assertEqual(self._count("agent prompt"), 1)
        self.assertEqual(self._count("agent start"), 1)


class WriteScopeTests(E2ECase):
    def test_write_without_a_declared_scope_is_refused(self) -> None:
        run = self._cli(
            "run", "--repo", str(self.repo), "--mode", "write", "edit the code"
        )
        self.assertEqual(run.returncode, 3, run.stderr)
        payload = self._json(run)
        self.assertEqual(payload["status"], "UNAVAILABLE")
        self.assertEqual(payload["error"]["code"], "PRODUCT_TASK_USAGE")
        # Nothing was recorded: the refusal happened before any task state.
        task_id = product_task.task_id_for(str(self.repo), "edit the code", [])
        self.assertFalse(
            os.path.exists(product_state.task_record_path(task_id, self.xdg))
        )

    def test_declared_write_scope_is_recorded_in_the_task_record(self) -> None:
        run = self._cli(
            "run",
            "--repo",
            str(self.repo),
            "--id",
            "twrite01",
            "--write",
            "a.txt",
            "edit the code",
        )
        record = product_state.read_task("twrite01", self.xdg)
        self.assertIsNotNone(record)
        self.assertEqual(record["mode"], "WRITE")
        self.assertIn("a.txt", record["scope"])
        # Through the CLI no live occupancy observation is available, so the
        # write gate fails closed; the scope is nonetheless recorded.
        payload = self._json(run)
        self.assertEqual(payload["status"], "refused")
        self.assertEqual(run.returncode, 3, run.stderr)


class FailClosedTests(E2ECase):
    def test_corrupt_task_record_fails_closed_and_is_not_absent(self) -> None:
        task_id = "tcorrupt01"
        path = product_state.task_record_path(task_id, self.xdg)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text("{ not json", encoding="utf-8")

        run = self._cli("run", "--repo", str(self.repo), "--id", task_id, "review prompt")
        self.assertEqual(run.returncode, 3, run.stderr)
        payload = self._json(run)
        self.assertEqual(payload["status"], "UNAVAILABLE")
        self.assertEqual(payload["error"]["code"], "TASK_STATE_UNREADABLE")
        # The corrupt bytes are untouched: never rounded down to "absent".
        self.assertEqual(Path(path).read_text(encoding="utf-8"), "{ not json")

    def test_missing_provider_fails_closed(self) -> None:
        no_provider_home = self.root / "no-provider-config"
        no_provider_home.mkdir()
        product_config.write_config(
            {"schema": product_config.SCHEMA},
            path=str(no_provider_home / "cao" / "config.json"),
        )
        env = self._cli_env()
        env["XDG_CONFIG_HOME"] = str(no_provider_home)

        run = self._cli("run", "--repo", str(self.repo), "review prompt", env=env)
        self.assertEqual(run.returncode, 3, run.stderr)
        payload = self._json(run)
        self.assertEqual(payload["status"], "UNAVAILABLE")
        self.assertEqual(payload["error"]["code"], "PRODUCT_TASK_UNAVAILABLE")

        doctor = self._cli("doctor", env=env)
        self.assertEqual(doctor.returncode, 3, doctor.stderr)
        self.assertEqual(self._json(doctor)["status"], "NOT_READY")

    def test_missing_runtime_binary_path_fails_closed(self) -> None:
        self._write_managed(self.root / "does-not-exist")
        run = self._cli("run", "--repo", str(self.repo), "review prompt")
        self.assertEqual(run.returncode, 3, run.stderr)
        payload = self._json(run)
        self.assertEqual(payload["status"], "UNAVAILABLE")
        self.assertEqual(payload["error"]["code"], "MANAGED_RUNTIME_NOT_READY")


class DaemonTickTests(E2ECase):
    def test_double_start_has_no_duplicate_effect(self) -> None:
        spawns: list[int] = []
        daemon = self._daemon(spawns)

        first = daemon.start()
        self.assertEqual(first["observed"], "running")
        self.assertFalse(first["already_running"])
        daemon.tick()

        second = daemon.start()
        self.assertTrue(second["already_running"])
        # Exactly one loop was ever spawned and durable progress was preserved.
        self.assertEqual(len(spawns), 1)
        self.assertEqual(product_state.read_daemon(self.xdg)["ticks"], 1)

    def test_stale_task_redrive_keeps_prompt_and_start_at_one(self) -> None:
        task = product_task.run_task(
            driver=self._driver(),
            repo=str(self.repo),
            prompt="review prompt",
            task_id="tdaemon01",
            config=self._config(),
            env=self.xdg,
            registry_db=self.db,
            wait_ms=0,
        )
        self.assertEqual(task["status"], "running")
        self.assertEqual(self._count("agent prompt"), 1)

        spawns: list[int] = []
        daemon = self._daemon(spawns)
        daemon.start()

        # The agent is still present: agreement, no effect.
        first = daemon.tick()
        self.assertEqual(first["effects"], [])

        # The agent vanishes: the task is stale and must be re-driven.
        self._drop_agents()
        daemon.tick()

        supervision = product_state.read_daemon(self.xdg)["supervision"]
        self.assertEqual(supervision["tdaemon01"]["action"], "redrive")
        # Idempotent resume: the re-drive never re-provisions, re-starts or
        # re-prompts.
        self.assertEqual(self._count("workspace create"), 1)
        self.assertEqual(self._count("agent start"), 1)
        self.assertEqual(self._count("agent prompt"), 1)


if __name__ == "__main__":
    unittest.main()
