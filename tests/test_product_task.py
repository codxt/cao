"""Tests for the product task lifecycle.

A fake driver records every call, and a sandboxed XDG root plus a sandboxed
registry ledger keep the tests hermetic: no live runtime, no network, and no
touch of the governed ledger. The suite pins idempotent resume (no duplicate
start or prompt), ambiguous-ack handling, WRITE scope refusal, registry
refusal surfacing, fail-closed corrupt state, and stop/release behaviour.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from cao import product_config, product_driver, product_state, product_task, registry
from cao.registry import RuntimeObservation
from cao.state import StateError

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


class FakeDriver:
    """Record calls and answer with deterministic runtime-shaped values."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.workspaces: list[dict] = []
        self.prompt_exc: Exception | None = None
        self.wait_state = "done"
        self.read_text = "agent output"
        self.agent_present = False

    def _note(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    def workspace_list(self):
        self._note("workspace_list")
        return list(self.workspaces)

    def workspace_create(self, label, cwd=None):
        self._note("workspace_create", label, cwd)
        self.workspaces.append(
            {"workspace_id": "w1", "label": label, "active_tab_id": "w1:t1"}
        )
        return {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1"}

    def pane_list(self, workspace_id):
        self._note("pane_list", workspace_id)
        return [{"pane_id": "w1:p1", "workspace_id": workspace_id}]

    def pane_get(self, pane_id):
        self._note("pane_get", pane_id)
        return {"pane_id": pane_id, "workspace_id": "w1"}

    def pane_close(self, pane_id):
        self._note("pane_close", pane_id)
        return "{}"

    def agent_start(self, name, kind, pane_id, *, argv=(), timeout_ms=None):
        self._note("agent_start", name, kind, pane_id, tuple(argv), timeout_ms)
        return {"agent_pane_id": pane_id, "agent_workspace_id": "w1"}

    def agent_prompt(self, target, text, *, timeout_ms=None, wait=False):
        self._note("agent_prompt", target, text)
        if self.prompt_exc is not None:
            raise self.prompt_exc
        return {"agent_pane_id": "w1:p1", "agent_workspace_id": "w1"}

    def agent_read(self, target, *, lines=200, source="recent"):
        self._note("agent_read", target)
        return self.read_text

    def agent_get(self, target):
        self._note("agent_get", target)
        if self.agent_present:
            return {"name": target, "agent_status": "idle"}
        raise product_driver.DriverNotFound("not present")

    def agent_wait(self, target, *, until=(), timeout_ms=None):
        self._note("agent_wait", target)
        return {"agent": {"agent_status": self.wait_state}}


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


class TaskCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "project"
        self.repo.mkdir()
        self.env = {
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
        }
        self.db = str(self.root / "registry.sqlite3")
        self.config = product_config.validate_config(
            {
                "schema": product_config.SCHEMA,
                "provider": {"cli": "opencode", "model": "m"},
            }
        )

    def run_read(self, driver=None, prompt="Review this repository", wait_ms=0):
        return product_task.run_task(
            driver=driver or FakeDriver(),
            repo=str(self.repo),
            prompt=prompt,
            config=self.config,
            env=self.env,
            registry_db=self.db,
            wait_ms=wait_ms,
        )


class IdentityTests(TaskCase):
    def test_task_id_is_deterministic_and_scope_sensitive(self) -> None:
        one = product_task.task_id_for(str(self.repo), "p")
        two = product_task.task_id_for(str(self.repo), "p")
        self.assertEqual(one, two)
        three = product_task.task_id_for(str(self.repo), "p", ["src/a.py"])
        self.assertNotEqual(one, three)

    def test_default_agent_kind_falls_back_to_provider_cli(self) -> None:
        defaults = product_config.task_defaults(self.config)
        self.assertEqual(defaults.agent_kind, "opencode")

    def test_task_defaults_fail_closed_on_a_bad_section(self) -> None:
        config = product_config.validate_config(
            {"schema": product_config.SCHEMA, "task": {"read_lines": -1}}
        )
        with self.assertRaises(product_config.ProductConfigError):
            product_config.task_defaults(config)

    def test_task_defaults_reject_an_unsupported_registry_attach(self) -> None:
        config = product_config.validate_config(
            {"schema": product_config.SCHEMA, "task": {"registry_attach": "private"}}
        )
        with self.assertRaises(product_config.ProductConfigError):
            product_config.task_defaults(config)


class ReadFlowTests(TaskCase):
    def test_read_task_claims_read_and_submits_once(self) -> None:
        driver = FakeDriver()
        payload = self.run_read(driver)
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["prompt_state"], "accepted")
        self.assertEqual(payload["claim"]["mode"], "READ")
        self.assertEqual(driver.names().count("agent_prompt"), 1)
        self.assertEqual(driver.names().count("workspace_create"), 1)
        conn = registry.connect(db=self.db)
        self.addCleanup(conn.close)
        rows = conn.execute("SELECT mode FROM claims").fetchall()
        self.assertEqual([row["mode"] for row in rows], ["READ"])

    def test_rerun_same_id_resumes_without_duplicating(self) -> None:
        driver = FakeDriver()
        task_id = product_task.task_id_for(str(self.repo), "Review this repository")
        first = product_task.run_task(
            driver=driver,
            repo=str(self.repo),
            prompt="Review this repository",
            task_id=task_id,
            config=self.config,
            env=self.env,
            registry_db=self.db,
            wait_ms=0,
        )
        second = product_task.run_task(
            driver=driver,
            repo=str(self.repo),
            prompt="Review this repository",
            task_id=task_id,
            config=self.config,
            env=self.env,
            registry_db=self.db,
            wait_ms=0,
        )
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(driver.names().count("agent_prompt"), 1)
        self.assertEqual(driver.names().count("agent_start"), 1)
        self.assertEqual(driver.names().count("workspace_create"), 1)

    def test_ambiguous_ack_is_unknown_and_never_reprompts(self) -> None:
        driver = FakeDriver()
        driver.prompt_exc = product_driver.DriverTransportError("send failed")
        task_id = product_task.task_id_for(str(self.repo), "Review this repository")
        first = product_task.run_task(
            driver=driver,
            repo=str(self.repo),
            prompt="Review this repository",
            task_id=task_id,
            config=self.config,
            env=self.env,
            registry_db=self.db,
            wait_ms=0,
        )
        self.assertEqual(first["status"], "unknown")
        self.assertEqual(first["prompt_state"], "ambiguous")
        driver.prompt_exc = None
        second = product_task.run_task(
            driver=driver,
            repo=str(self.repo),
            prompt="Review this repository",
            task_id=task_id,
            config=self.config,
            env=self.env,
            registry_db=self.db,
            wait_ms=0,
        )
        self.assertEqual(second["status"], "unknown")
        self.assertEqual(driver.names().count("agent_prompt"), 1)

    def test_start_recovery_does_not_duplicate_a_started_agent(self) -> None:
        task_id = product_task.task_id_for(str(self.repo), "Review this repository")
        record = {
            "schema": product_state.TASK_SCHEMA,
            "task_id": task_id,
            "run_id": product_config.DEFAULT_RUN_ID,
            "work_key": task_id,
            "repo": str(self.repo),
            "prompt": "Review this repository",
            "mode": "READ",
            "scope": [],
            "agent_kind": "opencode",
            "agent_args": [],
            "workspace_label": "cao-review-" + task_id,
            "status": "claimed",
            "checkpoint": {
                "workspace_id": "w1",
                "tab_id": "w1:t1",
                "pane_id": "w1:p1",
                "agent_name": "caoX",
                "agent_started": False,
                "start_attempted": True,
                "prompt_issued": False,
                "prompt_ack": "none",
                "stage": "start",
            },
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        product_state.write_task(record, self.env)
        driver = FakeDriver()
        driver.agent_present = True
        payload = product_task.run_task(
            driver=driver,
            repo=str(self.repo),
            prompt="Review this repository",
            task_id=task_id,
            config=self.config,
            env=self.env,
            registry_db=self.db,
            wait_ms=0,
        )
        self.assertEqual(payload["status"], "running")
        self.assertNotIn("agent_start", driver.names())
        self.assertIn("agent_get", driver.names())


class WriteAuthorityTests(TaskCase):
    def test_write_without_declared_scope_is_refused_before_any_call(self) -> None:
        driver = FakeDriver()
        with self.assertRaises(product_task.TaskUsageError):
            product_task.run_task(
                driver=driver,
                repo=str(self.repo),
                prompt="Edit the code",
                mode="write",
                write_scope=(),
                config=self.config,
                env=self.env,
                registry_db=self.db,
                wait_ms=0,
            )
        self.assertEqual(driver.calls, [])

    def test_overlapping_write_is_refused_by_the_ledger(self) -> None:
        repo = git_repo(self.root, name="gitrepo")
        observation = RuntimeObservation.empty()
        first = product_task.run_task(
            driver=FakeDriver(),
            repo=str(repo),
            prompt="Edit one",
            mode="write",
            write_scope=("a.txt",),
            observation=observation,
            config=self.config,
            env=self.env,
            registry_db=self.db,
            wait_ms=0,
        )
        self.assertEqual(first["status"], "running")
        self.assertEqual(first["claim"]["mode"], "WRITE")
        second_driver = FakeDriver()
        second = product_task.run_task(
            driver=second_driver,
            repo=str(repo),
            prompt="Edit two",
            mode="write",
            write_scope=("a.txt",),
            observation=observation,
            config=self.config,
            env=self.env,
            registry_db=self.db,
            wait_ms=0,
        )
        self.assertEqual(second["status"], "refused")
        self.assertEqual(second["refusal"]["status"], "CONFLICT")
        self.assertEqual(second_driver.calls, [])

    def test_read_task_refused_by_an_exclusive_holder_is_surfaced(self) -> None:
        conn = registry.connect(db=self.db)
        self.addCleanup(conn.close)
        registry.propose(conn, "other", "holder", str(self.repo), components=["x"])
        conn.execute(
            "INSERT INTO claims (run_id, work_key, issue, owner, mode, branch, "
            "worktree, claimed_at) VALUES (?,?,?,?,?,?,?,?)",
            ("other", "holder", "", "other-owner", "EXCLUSIVE", "", "", registry.now()),
        )
        driver = FakeDriver()
        payload = self.run_read(driver)
        self.assertEqual(payload["status"], "refused")
        self.assertEqual(payload["refusal"]["status"], "CONFLICT")
        self.assertEqual(driver.calls, [])


class CompletionAndStopTests(TaskCase):
    def test_done_wait_hands_back_complete_with_head(self) -> None:
        repo = git_repo(self.root, name="gitrepo2")
        driver = FakeDriver()
        driver.wait_state = "done"
        payload = product_task.run_task(
            driver=driver,
            repo=str(repo),
            prompt="Review this repository",
            config=self.config,
            env=self.env,
            registry_db=self.db,
            wait_ms=1000,
        )
        self.assertEqual(payload["status"], "complete")
        self.assertEqual(payload["claim"]["outcome"], "complete")
        self.assertEqual(len(payload["claim"]["released_head"]), 40)
        conn = registry.connect(db=self.db)
        self.addCleanup(conn.close)
        row = conn.execute(
            "SELECT outcome, released_head FROM claims WHERE work_key=?",
            (payload["task_id"],),
        ).fetchone()
        self.assertEqual(row["outcome"], "complete")
        self.assertEqual(row["released_head"], payload["claim"]["released_head"])

    def test_stop_releases_abandon_and_closes_only_our_pane(self) -> None:
        driver = FakeDriver()
        payload = self.run_read(driver)
        task_id = payload["task_id"]
        stopped = product_task.stop_task(
            driver=driver, task_id=task_id, env=self.env, registry_db=self.db
        )
        self.assertEqual(stopped["status"], "stopped")
        self.assertIn("pane_close", driver.names())
        conn = registry.connect(db=self.db)
        self.addCleanup(conn.close)
        row = conn.execute(
            "SELECT outcome, released_at FROM claims WHERE work_key=?", (task_id,)
        ).fetchone()
        self.assertEqual(row["outcome"], "abandon")
        self.assertIsNotNone(row["released_at"])

    def test_stop_without_a_task_is_unavailable(self) -> None:
        with self.assertRaises(product_task.TaskUnavailable):
            product_task.stop_task(env=self.env, registry_db=self.db)


class FailClosedTests(TaskCase):
    def test_corrupt_task_record_raises(self) -> None:
        task_id = product_task.task_id_for(str(self.repo), "Review this repository")
        path = product_state.task_record_path(task_id, self.env)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text("{ not json", encoding="utf-8")
        with self.assertRaises(StateError):
            product_task.run_task(
                driver=FakeDriver(),
                repo=str(self.repo),
                prompt="Review this repository",
                task_id=task_id,
                config=self.config,
                env=self.env,
                registry_db=self.db,
                wait_ms=0,
            )

    def test_logs_are_bounded_and_surfaced(self) -> None:
        payload = self.run_read()
        logs = product_task.read_logs(payload["task_id"], env=self.env, limit=5)
        self.assertTrue(logs["ok"])
        self.assertEqual(len(logs["output"]), 5)
        self.assertTrue(logs["truncated"])
        status = product_task.read_status(env=self.env)
        self.assertEqual(status["task_id"], payload["task_id"])


if __name__ == "__main__":
    unittest.main()
