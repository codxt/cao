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
from unittest.mock import patch
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
    """Record calls and answer with deterministic, production-shaped values.

    The shape mirrors the managed runtime the product drives: a pane reports
    the worker KIND (a type such as a CLI name) in ``agent`` plus its own
    ``agent_status``, while ``agent.get`` on this task's unique agent name
    reports that agent's own pane and workspace. Putting the unique name in the
    pane's ``agent`` field instead would hide the exact identity confusion the
    product must never make.
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.workspaces: list[dict] = []
        self.prompt_exc: Exception | None = None
        self.wait_state = "done"
        self.read_text = "agent output"
        self.agent_present = False
        self.closed_panes: set[str] = set()
        self.panes: dict[str, dict] = {}
        self.agents: dict[str, dict] = {}

    def _note(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    @staticmethod
    def _workspace_of(pane_id):
        return pane_id.split(":", 1)[0] if isinstance(pane_id, str) and ":" in pane_id else None

    def workspace_list(self):
        self._note("workspace_list")
        return list(self.workspaces)

    def workspace_create(self, label, cwd=None):
        self._note("workspace_create", label, cwd)
        self.workspaces.append(
            {"workspace_id": "w1", "label": label, "active_tab_id": "w1:t1"}
        )
        self.panes.setdefault("w1:p1", {
            "pane_id": "w1:p1", "workspace_id": "w1",
            "agent": None, "agent_status": "unknown",
        })
        return {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1"}

    def pane_list(self, workspace_id):
        self._note("pane_list", workspace_id)
        pane = self.panes.get("w1:p1", {})
        return [{
            "pane_id": "w1:p1", "workspace_id": workspace_id,
            "agent": pane.get("agent"), "agent_status": pane.get("agent_status"),
        }]

    def pane_get(self, pane_id):
        self._note("pane_get", pane_id)
        if pane_id in self.closed_panes:
            raise product_driver.DriverNotFound("closed pane")
        pane = self.panes.get(pane_id)
        if pane is None:
            return {"pane_id": pane_id, "workspace_id": self._workspace_of(pane_id),
                    "agent": None, "agent_status": "unknown"}
        return dict(pane)

    def pane_close(self, pane_id):
        self._note("pane_close", pane_id)
        self.closed_panes.add(pane_id)
        return "{}"

    def agent_start(self, name, kind, pane_id, *, argv=(), timeout_ms=None):
        self._note("agent_start", name, kind, pane_id, tuple(argv), timeout_ms)
        workspace_id = self._workspace_of(pane_id)
        pane = self.panes.setdefault(
            pane_id, {"pane_id": pane_id, "workspace_id": workspace_id})
        if workspace_id is not None:
            pane["workspace_id"] = workspace_id
        pane["agent"] = kind  # the worker KIND, never the unique name
        pane["agent_status"] = "working"
        self.agents[name] = {
            "name": name, "agent": kind, "pane_id": pane_id,
            "workspace_id": pane.get("workspace_id"),
        }
        return {"agent_pane_id": pane_id, "agent_workspace_id": pane.get("workspace_id")}

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
        agent = self.agents.get(target)
        if agent is not None:
            return dict(agent)
        if self.agent_present:
            return {"name": target, "agent": "opencode",
                    "pane_id": "w1:p1", "workspace_id": "w1"}
        raise product_driver.DriverNotFound("not present")

    def agent_wait(self, target, *, until=(), timeout_ms=None):
        self._note("agent_wait", target)
        return {"agent": {"agent_status": self.wait_state}}

    def drop_agents(self) -> None:
        """Model the runtime losing every started agent (an exited worker)."""
        self.agents.clear()
        self.agent_present = False
        for pane in self.panes.values():
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

    def claim_rows(self):
        conn = registry.connect(db=self.db)
        try:
            return conn.execute("SELECT * FROM claims").fetchall()
        finally:
            conn.close()


class IdentityTests(TaskCase):
    def test_task_id_is_deterministic_and_scope_sensitive(self) -> None:
        one = product_task.task_id_for(str(self.repo), "p")
        two = product_task.task_id_for(str(self.repo), "p")
        self.assertEqual(one, two)
        three = product_task.task_id_for(str(self.repo), "p", ["src/a.py"])
        self.assertNotEqual(one, three)

    def test_long_team_agent_names_bind_full_task_identity(self) -> None:
        left = "ct-client-readiness-recon-20261011-factory-read-work-0-abc00000"
        right = "ct-client-readiness-recon-20261011-brainux-read-work-0-def00000"
        first = product_task._agent_name(left)
        second = product_task._agent_name(right)
        self.assertNotEqual(first, second)
        self.assertEqual(first, product_task._agent_name(left))
        self.assertLessEqual(len(first), 32)
        self.assertTrue(first.startswith("cao"))
        self.assertTrue(second.startswith("cao"))

    def test_default_agent_kind_falls_back_to_provider_cli(self) -> None:
        defaults = product_config.task_defaults(self.config)
        self.assertEqual(defaults.agent_kind, "opencode")

    def test_opencode_provider_model_selects_mini_by_default(self) -> None:
        defaults = product_config.task_defaults(self.config)
        self.assertEqual(
            defaults.agent_args,
            ("mini", "--standalone", "--model", "m"),
        )
        driver = FakeDriver()
        self.run_read(driver)
        start = next(call for call in driver.calls if call[0] == "agent_start")
        self.assertEqual(start[1][3], defaults.agent_args)

    def test_explicit_agent_args_override_opencode_default(self) -> None:
        config = product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "model": "pinned"},
            "task": {"agent_args": ["mini", "--model", "pinned"]},
        })
        self.assertEqual(
            product_config.task_defaults(config).agent_args,
            ("mini", "--model", "pinned"),
        )

    def test_explicit_opencode_args_cannot_change_or_omit_configured_model(self) -> None:
        for arguments in ([], ["mini", "--model", "different"], ["--model", "pinned"]):
            with self.subTest(arguments=arguments):
                config = product_config.validate_config({
                    "schema": product_config.SCHEMA,
                    "provider": {"cli": "opencode", "model": "pinned"},
                    "task": {"agent_args": arguments},
                })
                with self.assertRaises(product_config.ProductConfigError):
                    product_config.task_defaults(config)

    def test_non_opencode_provider_args_remain_explicit(self) -> None:
        config = product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "another-agent", "model": "pinned"},
        })
        self.assertEqual(product_config.task_defaults(config).agent_args, ())

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

    def test_idle_after_prompt_is_not_completion_and_does_not_release_read(self) -> None:
        # The live runtime can acknowledge prompt while still reporting idle.
        # A caller must not advance task dependencies before a real DONE.
        driver = FakeDriver()
        driver.wait_state = "idle"
        repo = git_repo(self.root, name="tracked-read")
        args = dict(driver=driver, repo=str(repo), prompt="Read tracked repo",
                    config=self.config, env=self.env,
                    registry_db=self.db, wait_ms=1000)
        first = product_task.run_task(**args)
        self.assertEqual(first["status"], "running")
        self.assertFalse(first["claim"].get("released", False))
        self.assertEqual(driver.names().count("agent_prompt"), 1)
        driver.wait_state = "done"
        second = product_task.run_task(**args)
        self.assertEqual(second["status"], "complete")
        self.assertTrue(second["claim"]["released"])
        self.assertEqual(driver.names().count("agent_prompt"), 1)
        calls = [call for call in driver.calls if call[0] == "agent_wait"]
        self.assertTrue(calls)
        self.assertEqual(len(calls), 2)

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

    def test_write_resume_after_committed_work_reuses_exact_claim(self) -> None:
        repo = git_repo(self.root, name="resume-committed")
        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                       capture_output=True, env={**os.environ, **GIT_ENV})
        def git(*args):
            return subprocess.run(
                ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
                check=True, capture_output=True, text=True,
                env={**os.environ, **GIT_ENV},
            )
        git("remote", "add", "origin", str(remote))
        git("push", "--set-upstream", "origin", "main")
        driver = FakeDriver()
        args = dict(
            driver=driver, repo=str(repo), prompt="Edit this file",
            task_id="resume-write", mode="write", write_scope=("a.txt",),
            observation=RuntimeObservation.empty(), config=self.config,
            env=self.env, registry_db=self.db,
        )
        first = product_task.run_task(**args, wait_ms=0)
        self.assertEqual(first["status"], "running")
        self.assertEqual(first["claim"]["mode"], "WRITE")
        (repo / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
        git("add", "a.txt")
        git("commit", "-m", "Accepted local change")
        head = git("rev-parse", "HEAD").stdout.strip()
        # A previous preflight on its own already-owned commit could persist a
        # false refused state. Recovery must re-use the same live claim.
        record = product_state.read_task("resume-write", self.env)
        record["status"] = "refused"
        record["refusal"] = {"stage": "preflight", "status": "CONFLICT"}
        product_state.write_task(record, self.env)
        after = product_task.run_task(**args, wait_ms=1000)
        self.assertIsNone(after["refusal"])
        self.assertEqual(after["status"], "complete")
        self.assertTrue(after["claim"]["released"])
        self.assertEqual(after["claim"]["released_head"], head)
        self.assertEqual(driver.names().count("agent_start"), 1)
        self.assertEqual(driver.names().count("agent_prompt"), 1)
        conn = registry.connect(db=self.db)
        try:
            rows = conn.execute(
                "SELECT claim_id, outcome, released_head FROM claims "
                "WHERE work_key=?", ("resume-write",),
            ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["outcome"], "complete")
            self.assertEqual(rows[0]["released_head"], head)
        finally:
            conn.close()

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


class WriteCompletionScopeTests(TaskCase):
    """Actual Git side effects must be proven inside the held WRITE scope."""

    def setUp(self) -> None:
        super().setUp()
        self.write_repo = git_repo(self.root, name="write-scope", files=("a.txt", "other.txt"))
        remote = self.root / "upstream.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                       capture_output=True, env={**os.environ, **GIT_ENV})
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "--set-upstream", "origin", "main")
        self.driver = FakeDriver()
        self.kwargs = dict(
            driver=self.driver, repo=str(self.write_repo), prompt="Edit a.txt",
            task_id="scope-case", mode="write", write_scope=("a.txt",),
            observation=RuntimeObservation.empty(), config=self.config,
            env=self.env, registry_db=self.db,
        )

    def git(self, *arguments: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.write_repo), "-c", "commit.gpgsign=false", *arguments],
            capture_output=True, text=True, check=True,
            env={**os.environ, **GIT_ENV},
        )
        return proc.stdout.strip()

    def begin(self):
        result = product_task.run_task(**self.kwargs, wait_ms=0)
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["claim"]["mode"], "WRITE")
        return result

    def finish(self):
        return product_task.run_task(**self.kwargs, wait_ms=1000)

    def assert_claim_open(self):
        conn = registry.connect(db=self.db)
        try:
            row = conn.execute("SELECT released_at FROM claims WHERE work_key='scope-case'").fetchone()
            self.assertIsNone(row["released_at"])
        finally:
            conn.close()
        self.assertEqual(self.driver.names().count("agent_prompt"), 1)
        self.assertEqual(self.driver.names().count("agent_start"), 1)

    def test_repeated_runs_never_duplicate_a_write_claim_or_prompt(self):
        """Falsification 7: retained observations never re-claim or re-prompt."""
        first = self.begin()
        resumed = product_task.run_task(**self.kwargs, wait_ms=0)
        self.assertEqual(resumed["status"], "running")
        self.assertEqual(resumed["claim"]["claim_id"],
                         first["claim"]["claim_id"])
        self.assertEqual(self.driver.names().count("agent_start"), 1)
        self.assertEqual(self.driver.names().count("agent_prompt"), 1)
        record = product_state.read_task("scope-case", self.env)
        self.assertTrue(record["output_observations"])
        self.assertTrue(all(entry["post_prompt"]
                            for entry in record["output_observations"]))
        conn = registry.connect(db=self.db)
        try:
            rows = conn.execute(
                "SELECT claim_id FROM claims WHERE work_key='scope-case'"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)

    def test_out_of_scope_commit_cannot_complete_and_retains_claim(self):
        self.begin()
        (self.write_repo / "unscoped.txt").write_text("escaped\n")
        self.git("add", "unscoped.txt")
        self.git("commit", "-m", "out of scope")
        result = self.finish()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error"]["code"], "TASK_WRITE_SCOPE_VIOLATION")
        self.assert_claim_open()
        again = self.finish()
        self.assertEqual(again["status"], "blocked")
        self.assert_claim_open()

    def test_no_write_commit_cannot_claim_success(self):
        before = self.git("rev-parse", "HEAD")
        self.begin()
        result = self.finish()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error"]["code"], "TASK_WRITE_RESULT_UNVERIFIED")
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assert_claim_open()

    def test_dirty_write_must_be_committed_before_release(self):
        self.begin()
        (self.write_repo / "a.txt").write_text("one\ntwo\n")
        result = self.finish()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error"]["code"], "TASK_WRITE_RESULT_UNVERIFIED")
        self.assert_claim_open()

    def test_write_finish_closes_own_pane_before_release(self):
        self.begin()
        (self.write_repo / "a.txt").write_text("committed\n")
        self.git("add", "a.txt")
        self.git("commit", "-m", "task output")
        result = self.finish()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["pane_closed"], "w1:p1")
        self.assertEqual(self.driver.names().count("pane_close"), 1)
        self.assert_claim_released()

    def test_write_retries_release_after_verified_closed_pane(self):
        class ClosedWait(FakeDriver):
            def agent_wait(self, target, *, until=(), timeout_ms=None):
                if "w1:p1" in self.closed_panes:
                    raise product_driver.DriverNotFound("closed worker")
                return super().agent_wait(target, until=until, timeout_ms=timeout_ms)

        self.driver = ClosedWait()
        self.kwargs["driver"] = self.driver
        self.begin()
        (self.write_repo / "a.txt").write_text("committed\n")
        self.git("add", "a.txt")
        self.git("commit", "-m", "task output")
        with patch.object(registry, "release", return_value={
            "ok": False, "errors": ["temporary ledger refusal"],
        }):
            first = self.finish()
        self.assertEqual(first["status"], "running")
        self.assertEqual(first["error"]["code"], "TASK_CLAIM_RELEASE_REJECTED")
        self.assertEqual(first["pane_closed"], "w1:p1")
        self.assert_claim_open()
        second = self.finish()
        self.assertEqual(second["status"], "complete")
        self.assertEqual(self.driver.names().count("agent_start"), 1)
        self.assertEqual(self.driver.names().count("agent_prompt"), 1)
        self.assertEqual(self.driver.names().count("pane_close"), 1)
        self.assert_claim_released()

    def test_async_close_then_not_found_recovers_same_write_claim(self):
        class DelayedClose(FakeDriver):
            def __init__(self):
                super().__init__()
                self.close_requested = False
                self.gone = False

            def pane_close(self, pane_id):
                self._note("pane_close", pane_id)
                self.close_requested = True
                return "{}"  # Ack before actual teardown

            def pane_get(self, pane_id):
                if self.gone:
                    self._note("pane_get", pane_id)
                    raise product_driver.DriverNotFound("pane gone")
                return super().pane_get(pane_id)

            def agent_wait(self, target, *, until=(), timeout_ms=None):
                if self.close_requested:
                    self.gone = True
                    raise product_driver.DriverNotFound("agent has exited")
                return super().agent_wait(target, until=until, timeout_ms=timeout_ms)

        self.driver = DelayedClose()
        self.kwargs["driver"] = self.driver
        self.begin()
        (self.write_repo / "a.txt").write_text("committed\n")
        self.git("add", "a.txt")
        self.git("commit", "-m", "task output")
        first = self.finish()
        self.assertEqual(first["status"], "blocked")
        self.assertEqual(first["error"]["code"], "TASK_PANE_CLOSE_FAILED")
        self.assert_claim_open()
        second = self.finish()
        self.assertEqual(second["status"], "complete")
        self.assertEqual(self.driver.names().count("agent_start"), 1)
        self.assertEqual(self.driver.names().count("agent_prompt"), 1)
        self.assertEqual(self.driver.names().count("pane_close"), 1)
        self.assert_claim_released()

    def test_noop_pane_close_never_releases_write(self):
        class NoopClose(FakeDriver):
            def pane_close(self, pane_id):
                self._note("pane_close", pane_id)
                return "{}"

        self.driver = NoopClose()
        self.kwargs["driver"] = self.driver
        self.begin()
        (self.write_repo / "a.txt").write_text("committed\n")
        self.git("add", "a.txt")
        self.git("commit", "-m", "task output")
        result = self.finish()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error"]["code"], "TASK_PANE_CLOSE_FAILED")
        self.assert_claim_open()

    def test_write_finish_with_unclosed_pane_keeps_claim(self):
        class RefusingClose(FakeDriver):
            def pane_close(self, pane_id):
                self._note("pane_close", pane_id)
                raise product_driver.DriverError("runtime refused close")

        self.driver = RefusingClose()
        self.kwargs["driver"] = self.driver
        self.begin()
        (self.write_repo / "a.txt").write_text("committed\n")
        self.git("add", "a.txt")
        self.git("commit", "-m", "task output")
        result = self.finish()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error"]["code"], "TASK_PANE_CLOSE_FAILED")
        self.assert_claim_open()

    def test_in_scope_commit_reuses_claim_and_finishes(self):
        original = self.git("rev-parse", "HEAD")
        self.begin()
        record = product_state.read_task("scope-case", self.env)
        self.assertEqual(record["claim"]["base_head"], original)
        self.assertEqual(record["claim"]["base_branch"], "main")
        (self.write_repo / "a.txt").write_text("one\ntwo\n")
        self.git("add", "a.txt")
        self.git("commit", "-m", "within scope")
        head = self.git("rev-parse", "HEAD")
        result = self.finish()
        self.assertEqual(result["status"], "complete")
        self.assertTrue(result["claim"]["released"])
        self.assertEqual(result["claim"]["released_head"], head)
        self.assert_claim_released()
        self.assertEqual(self.driver.names().count("agent_prompt"), 1)

    def assert_claim_released(self):
        conn = registry.connect(db=self.db)
        try:
            row = conn.execute("SELECT released_at FROM claims WHERE work_key='scope-case'").fetchone()
            self.assertIsNotNone(row["released_at"])
        finally:
            conn.close()

    def test_preexisting_history_outside_scope_not_counted_as_task_effect(self):
        (self.write_repo / "other.txt").write_text("prior history\n")
        self.git("add", "other.txt")
        self.git("commit", "-m", "before task")
        self.git("push", "origin", "main")  # prior work is already accepted baseline
        base = self.git("rev-parse", "HEAD")
        self.begin()
        (self.write_repo / "a.txt").write_text("in scope\n")
        self.git("add", "a.txt")
        self.git("commit", "-m", "task effect")
        result = self.finish()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(product_state.read_task("scope-case", self.env)["claim"]["base_head"], base)

    def test_branch_drift_cannot_complete(self):
        self.begin()
        self.git("checkout", "-b", "other-branch")
        (self.write_repo / "a.txt").write_text("new content\n")
        self.git("add", "a.txt")
        self.git("commit", "-m", "wrong branch")
        result = self.finish()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error"]["code"], "TASK_WRITE_SCOPE_VIOLATION")
        self.assert_claim_open()

    def test_original_claim_worktree_cannot_be_rebound_to_other_repo(self):
        self.begin()
        conn = registry.connect(db=self.db)
        try:
            conn.execute("UPDATE claims SET worktree=? WHERE work_key=?", ("/tmp/other-repo", "scope-case"))
        finally:
            conn.close()
        result = self.finish()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error"]["code"], "TASK_WRITE_SCOPE_VIOLATION")
        self.assert_claim_open()


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

    def test_read_finish_closes_own_pane_before_release(self) -> None:
        # An ordinary READ completion must close its own pane before releasing
        # the claim, exactly like a WRITE, so a finished reader is never left
        # with a leaked pane.
        repo = git_repo(self.root, name="read-close")
        driver = FakeDriver()
        driver.wait_state = "done"
        payload = product_task.run_task(
            driver=driver, repo=str(repo), prompt="Read the repository",
            config=self.config, env=self.env, registry_db=self.db, wait_ms=1000,
        )
        self.assertEqual(payload["status"], "complete")
        self.assertEqual(payload["pane_closed"], "w1:p1")
        self.assertTrue(payload["claim"]["released"])
        self.assertEqual(payload["claim"]["outcome"], "complete")
        self.assertEqual(driver.names().count("pane_close"), 1)

    def test_pane_kind_is_never_compared_to_the_unique_agent_name(self) -> None:
        # A real pane reports the worker KIND in ``agent`` (e.g. the CLI name)
        # while ``agent.get`` resolves this task's unique agent name. The close
        # must bind through the named agent, never mistake the kind for a name.
        repo = git_repo(self.root, name="kind-vs-name")
        driver = FakeDriver()
        driver.wait_state = "done"
        payload = product_task.run_task(
            driver=driver, repo=str(repo), prompt="Read the repository",
            config=self.config, env=self.env, registry_db=self.db, wait_ms=1000,
        )
        self.assertEqual(payload["status"], "complete")
        name, kind = next(
            call[1] for call in driver.calls if call[0] == "agent_start")[:2]
        self.assertNotEqual(name, kind)          # unique name vs worker kind
        self.assertEqual(driver.panes["w1:p1"]["agent"], kind)
        self.assertEqual(payload["pane_closed"], "w1:p1")
        self.assertTrue(payload["claim"]["released"])

    def test_matching_kind_with_a_foreign_named_agent_holds(self) -> None:
        # The pane's kind matches the named agent's kind, but ``agent.get``
        # resolves this task's own name to a different pane: the close must hold
        # the claim rather than touch a reused pane.
        class ReusedPane(FakeDriver):
            def agent_get(self, target):
                agent = super().agent_get(target)
                return dict(agent, pane_id="w9:p9")

        repo = git_repo(self.root, name="reused-pane")
        driver = ReusedPane()
        driver.wait_state = "done"
        payload = product_task.run_task(
            driver=driver, repo=str(repo), prompt="Read the repository",
            config=self.config, env=self.env, registry_db=self.db, wait_ms=1000,
        )
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["error"]["code"], "TASK_PANE_CLOSE_FAILED")
        self.assertNotIn("pane_close", driver.names())
        self.assertFalse(payload["claim"].get("released"))

    def test_read_finish_holds_claim_when_pane_close_fails(self) -> None:
        class FailingClose(FakeDriver):
            def pane_close(self, pane_id):
                self._note("pane_close", pane_id)
                raise product_driver.DriverError("close unavailable")

        repo = git_repo(self.root, name="read-close-fail")
        driver = FailingClose()
        driver.wait_state = "done"
        payload = product_task.run_task(
            driver=driver, repo=str(repo), prompt="Read the repository",
            config=self.config, env=self.env, registry_db=self.db, wait_ms=1000,
        )
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["error"]["code"], "TASK_PANE_CLOSE_FAILED")
        self.assertFalse(payload["claim"].get("released"))
        conn = registry.connect(db=self.db)
        try:
            row = conn.execute(
                "SELECT released_at FROM claims WHERE work_key=?",
                (payload["task_id"],)).fetchone()
            self.assertIsNone(row["released_at"])
        finally:
            conn.close()

    def test_completion_gate_refusal_never_releases_or_closes(self) -> None:
        # The optional completion gate runs on the durable terminal evidence
        # BEFORE pane closure and claim release, so a refused result holds both.
        repo = git_repo(self.root, name="gate-refusal")
        driver = FakeDriver()
        driver.wait_state = "done"
        calls: list = []

        def refuse(record):
            calls.append(bool(record.get("output_observations")))
            return {"code": "TEAM_DURABLE_RESULT_UNVERIFIED",
                    "message": "no exact receipt"}

        payload = product_task.run_task(
            driver=driver, repo=str(repo), prompt="Read the repository",
            config=self.config, env=self.env, registry_db=self.db,
            wait_ms=1000, verify=refuse,
        )
        self.assertEqual(payload["status"], "unknown")
        self.assertEqual(payload["error"]["code"], "TEAM_DURABLE_RESULT_UNVERIFIED")
        self.assertFalse(payload["claim"].get("released"))
        self.assertIsNone(payload.get("pane_closed"))
        self.assertNotIn("pane_close", driver.names())
        # The gate saw the durable terminal observation it was asked to judge.
        self.assertTrue(calls and calls[0])

    def test_completion_gate_acceptance_still_releases_and_closes(self) -> None:
        repo = git_repo(self.root, name="gate-accept")
        driver = FakeDriver()
        driver.wait_state = "done"
        seen: list[bool] = []

        def accept(record):
            seen.append(bool(record.get("output") is not None))
            return None

        payload = product_task.run_task(
            driver=driver, repo=str(repo), prompt="Read the repository",
            config=self.config, env=self.env, registry_db=self.db,
            wait_ms=1000, verify=accept,
        )
        self.assertEqual(payload["status"], "complete")
        self.assertTrue(seen)
        self.assertEqual(payload["pane_closed"], "w1:p1")
        self.assertTrue(payload["claim"]["released"])

    def test_read_retries_release_after_verified_closed_pane(self) -> None:
        # A non-atomic crash between pane closure and claim release: the pane is
        # already gone but the claim is open. The re-run only completes because
        # the recorded close matches the exact owned pane, which is verifiably
        # absent; it never re-prompts or starts a replacement.
        class ClosedWait(FakeDriver):
            def agent_wait(self, target, *, until=(), timeout_ms=None):
                if "w1:p1" in self.closed_panes:
                    raise product_driver.DriverNotFound("closed worker")
                return super().agent_wait(
                    target, until=until, timeout_ms=timeout_ms)

        repo = git_repo(self.root, name="read-retry-release")
        driver = ClosedWait()
        driver.wait_state = "done"
        args = dict(driver=driver, repo=str(repo), prompt="Read the repository",
                    config=self.config, env=self.env, registry_db=self.db)
        with patch.object(registry, "release", return_value={
                "ok": False, "errors": ["temporary ledger refusal"]}):
            first = product_task.run_task(**args, wait_ms=1000)
        self.assertEqual(first["status"], "running")
        self.assertEqual(first["error"]["code"], "TASK_CLAIM_RELEASE_REJECTED")
        self.assertEqual(first["pane_closed"], "w1:p1")
        second = product_task.run_task(**args, wait_ms=1000)
        self.assertEqual(second["status"], "complete")
        self.assertTrue(second["claim"]["released"])
        self.assertEqual(driver.names().count("pane_close"), 1)
        self.assertEqual(driver.names().count("agent_prompt"), 1)

    def test_read_post_close_recovery_needs_the_matching_checkpoint(self) -> None:
        # A recorded close for a DIFFERENT pane is not post-close evidence for
        # this task, so with the worker gone the READ holds rather than
        # releasing on a bare guess.
        class GoneWorker(FakeDriver):
            def agent_wait(self, target, *, until=(), timeout_ms=None):
                raise product_driver.DriverNotFound("worker gone")

        repo = git_repo(self.root, name="read-close-mismatch")
        driver = GoneWorker()
        started = product_task.run_task(
            driver=driver, repo=str(repo), prompt="Read the repository",
            config=self.config, env=self.env, registry_db=self.db, wait_ms=0)
        self.assertEqual(started["status"], "running")
        record = product_state.read_task(started["task_id"], self.env)
        record["pane_closed"] = "w9:p9"
        product_state.write_task(record, self.env)
        resumed = product_task.run_task(
            driver=driver, repo=str(repo), prompt="Read the repository",
            task_id=started["task_id"], config=self.config, env=self.env,
            registry_db=self.db, wait_ms=1000)
        self.assertEqual(resumed["status"], "running")
        self.assertFalse(resumed["claim"].get("released"))
        self.assertNotIn("pane_close", driver.names())

    def _release_out_of_band(self, payload):
        conn = registry.connect(db=self.db)
        try:
            released = registry.release(
                conn, product_config.DEFAULT_RUN_ID, product_task.OWNER,
                payload["task_id"], issue="", outcome="abandon",
            )
            self.assertTrue(released["ok"])
        finally:
            conn.close()

    def _resume_for_finish(self, repo, payload, driver):
        return product_task.run_task(
            driver=driver, repo=str(repo), prompt="Review this repository",
            task_id=payload["task_id"], config=self.config, env=self.env,
            registry_db=self.db, wait_ms=1000,
        )

    def test_finish_with_rejected_release_is_not_complete(self) -> None:
        repo = git_repo(self.root, name="finish-refusal")
        driver = FakeDriver()
        started = product_task.run_task(
            driver=driver, repo=str(repo), prompt="Review this repository",
            config=self.config, env=self.env, registry_db=self.db, wait_ms=0,
        )
        self.assertEqual(started["status"], "running")
        self._release_out_of_band(started)
        finished = self._resume_for_finish(repo, started, driver)
        self.assertEqual(finished["status"], "running")
        self.assertEqual(finished["error"]["code"], "TASK_CLAIM_RELEASE_REJECTED")
        self.assertFalse(finished["claim"].get("released"))
        conn = registry.connect(db=self.db)
        try:
            row = conn.execute("SELECT outcome FROM claims WHERE work_key=?", (started["task_id"],)).fetchone()
            self.assertEqual(row["outcome"], "abandon")
        finally:
            conn.close()
        self.assertEqual(driver.names().count("agent_prompt"), 1)

    def test_finish_rejected_release_during_wait_is_not_complete(self) -> None:
        repo = git_repo(self.root, name="finish-race")
        driver = FakeDriver()
        with patch.object(registry, "release", return_value={
            "ok": False, "errors": ["claim already released elsewhere"],
        }):
            finished = product_task.run_task(
                driver=driver, repo=str(repo), prompt="Review this repository",
                config=self.config, env=self.env, registry_db=self.db,
                wait_ms=1000,
            )
        self.assertEqual(finished["status"], "running")
        self.assertEqual(finished["error"]["code"], "TASK_CLAIM_RELEASE_REJECTED")
        self.assertFalse(finished["claim"].get("released"))
        self.assertEqual(driver.names().count("agent_prompt"), 1)
        conn = registry.connect(db=self.db)
        try:
            row = conn.execute(
                "SELECT released_at FROM claims WHERE work_key=?", (finished["task_id"],)
            ).fetchone()
            self.assertIsNone(row["released_at"])
        finally:
            conn.close()

    def test_stop_with_rejected_release_stays_running_and_keeps_pane(self) -> None:
        driver = FakeDriver()
        started = self.run_read(driver)
        self._release_out_of_band(started)
        stopped = product_task.stop_task(
            driver=driver, task_id=started["task_id"], env=self.env, registry_db=self.db,
        )
        self.assertEqual(stopped["status"], "running")
        self.assertEqual(stopped["error"]["code"], "TASK_CLAIM_RELEASE_REJECTED")
        self.assertNotIn("pane_close", driver.names())
        self.assertNotEqual(stopped["claim"].get("status"), "RELEASED")

    def test_stop_with_failed_pane_close_stays_running(self) -> None:
        class FailingClose(FakeDriver):
            def pane_close(self, pane_id):
                self._note("pane_close", pane_id)
                raise product_driver.DriverError("close unavailable")

        driver = FailingClose()
        started = self.run_read(driver)
        first = product_task.stop_task(
            driver=driver, task_id=started["task_id"], env=self.env, registry_db=self.db,
        )
        self.assertEqual(first["status"], "running")
        self.assertEqual(first["error"]["code"], "TASK_PANE_CLOSE_FAILED")
        self.assertFalse(first["claim"].get("released"))
        self.assertIsNone(first.get("pane_closed"))
        conn = registry.connect(db=self.db)
        try:
            row = conn.execute("SELECT outcome, released_at FROM claims WHERE work_key=?", (started["task_id"],)).fetchone()
            self.assertIsNone(row["outcome"])
            self.assertIsNone(row["released_at"])
        finally:
            conn.close()
        fixed = product_task.stop_task(
            driver=FakeDriver(), task_id=started["task_id"], env=self.env, registry_db=self.db,
        )
        self.assertEqual(fixed["status"], "stopped")
        self.assertEqual(fixed.get("pane_closed"), "w1:p1")

    def test_retry_after_rejected_finish_issues_no_second_prompt(self) -> None:
        repo = git_repo(self.root, name="retry-refusal")
        driver = FakeDriver()
        started = product_task.run_task(
            driver=driver, repo=str(repo), prompt="Review this repository",
            config=self.config, env=self.env, registry_db=self.db, wait_ms=0,
        )
        self._release_out_of_band(started)
        first = self._resume_for_finish(repo, started, driver)
        second = self._resume_for_finish(repo, started, driver)
        self.assertEqual(first["status"], "running")
        self.assertEqual(second["status"], "running")
        self.assertEqual(second["error"]["code"], "TASK_CLAIM_RELEASE_REJECTED")
        self.assertEqual(driver.names().count("agent_prompt"), 1)

    def test_failed_close_retains_write_exclusivity(self) -> None:
        class FailingClose(FakeDriver):
            def pane_close(self, pane_id):
                self._note("pane_close", pane_id)
                raise product_driver.DriverError("close failed")

        repo = git_repo(self.root, name="write-stop-race")
        driver = FailingClose()
        first = product_task.run_task(
            driver=driver, repo=str(repo), prompt="Edit first",
            mode="write", write_scope=("a.txt",),
            observation=RuntimeObservation.empty(),
            config=self.config, env=self.env, registry_db=self.db, wait_ms=0,
        )
        self.assertEqual(first["status"], "running")
        incomplete = product_task.stop_task(
            driver=driver, task_id=first["task_id"], env=self.env, registry_db=self.db,
        )
        self.assertEqual(incomplete["status"], "running")
        self.assertFalse(incomplete["claim"].get("released"))
        other = FakeDriver()
        second = product_task.run_task(
            driver=other, repo=str(repo), prompt="Edit second",
            mode="write", write_scope=("a.txt",),
            observation=RuntimeObservation.empty(),
            config=self.config, env=self.env, registry_db=self.db, wait_ms=0,
        )
        self.assertEqual(second["status"], "refused")
        self.assertEqual(second["refusal"]["status"], "CONFLICT")
        self.assertNotIn("agent_start", other.names())
        self.assertNotIn("agent_prompt", other.names())

    def test_stop_with_noop_pane_close_retains_claim(self) -> None:
        class NoopClose(FakeDriver):
            def pane_close(self, pane_id):
                self._note("pane_close", pane_id)
                return "{}"

        driver = NoopClose()
        started = self.run_read(driver)
        stopped = product_task.stop_task(
            driver=driver, task_id=started["task_id"], env=self.env, registry_db=self.db,
        )
        self.assertEqual(stopped["status"], "running")
        self.assertEqual(stopped["error"]["code"], "TASK_PANE_CLOSE_FAILED")
        self.assertFalse(stopped["claim"].get("released"))

    def test_stop_wrong_workspace_must_keep_claim(self) -> None:
        class ForeignPane(FakeDriver):
            def pane_get(self, pane_id):
                self._note("pane_get", pane_id)
                return {"pane_id": pane_id, "workspace_id": "another-workspace"}

        driver = ForeignPane()
        started = self.run_read(driver)
        result = product_task.stop_task(
            driver=driver, task_id=started["task_id"], env=self.env, registry_db=self.db,
        )
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["error"]["code"], "TASK_PANE_CLOSE_FAILED")
        self.assertNotIn("pane_close", driver.names())
        self.assertFalse(result["claim"].get("released"))

    def test_stop_missing_workspace_identity_must_keep_claim(self) -> None:
        class UnprovenPane(FakeDriver):
            def pane_get(self, pane_id):
                self._note("pane_get", pane_id)
                return {"pane_id": pane_id, "workspace_id": None}

        driver = UnprovenPane()
        started = self.run_read(driver)
        record = product_state.read_task(started["task_id"], self.env)
        record["checkpoint"]["workspace_id"] = None
        product_state.write_task(record, self.env)
        result = product_task.stop_task(
            driver=driver, task_id=started["task_id"], env=self.env, registry_db=self.db,
        )
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["error"]["code"], "TASK_PANE_CLOSE_FAILED")
        self.assertNotIn("pane_close", driver.names())
        self.assertFalse(result["claim"].get("released"))

    def test_stop_without_runtime_does_not_fake_pane_close(self) -> None:
        driver = FakeDriver()
        started = self.run_read(driver)
        first = product_task.stop_task(
            driver=None, task_id=started["task_id"], env=self.env, registry_db=self.db,
        )
        self.assertEqual(first["status"], "running")
        self.assertFalse(first["claim"].get("released"))
        self.assertEqual(first["error"]["code"], "TASK_PANE_CLOSE_FAILED")
        retry = product_task.stop_task(
            driver=driver, task_id=started["task_id"], env=self.env, registry_db=self.db,
        )
        self.assertEqual(retry["status"], "stopped")
        self.assertEqual(retry["pane_closed"], "w1:p1")

    def test_stop_without_a_task_is_unavailable(self) -> None:
        with self.assertRaises(product_task.TaskUnavailable):
            product_task.stop_task(env=self.env, registry_db=self.db)


class FocusedIdleAdmissionTests(TaskCase):
    """A focused tab reports IDLE for the same settled state an unseen
    background worker reports as DONE. A team execution (one that supplies a
    completion gate) may therefore settle at EITHER, but a bare idle is never
    completion: an idle is admitted only when this execution's own verified,
    task-bound terminal result passes the gate. An idle that does not is an
    early/transient idle and stays RUNNING with its claim held.
    """

    RECEIPT = "CAO_TEAM_RESULT: " + ("a" * 16)

    def gated(self, driver, *, repo_name, prompt="Run the task", wait_ms=1000,
              verify=None):
        repo = git_repo(self.root, name=repo_name)
        return product_task.run_task(
            driver=driver, repo=str(repo), prompt=prompt, config=self.config,
            env=self.env, registry_db=self.db, wait_ms=wait_ms, verify=verify,
        )

    def test_focused_idle_with_a_verified_result_completes_and_releases(self) -> None:
        driver = FakeDriver()
        driver.wait_state = "idle"
        driver.read_text = "final answer\n" + self.RECEIPT
        seen: list[bool] = []

        def accept(record):
            observations = record.get("output_observations") or []
            seen.append(bool(observations) and observations[-1]["terminal"] is True)
            return None

        payload = self.gated(driver, repo_name="idle-accept", verify=accept)
        self.assertEqual(payload["status"], "complete")
        self.assertEqual(payload["pane_closed"], "w1:p1")
        self.assertTrue(payload["claim"]["released"])
        # The gate judged a durable capture that was marked terminal.
        self.assertEqual(seen, [True])

    def test_immediate_idle_without_a_verified_result_stays_running(self) -> None:
        driver = FakeDriver()
        driver.wait_state = "idle"
        driver.read_text = "no completion line here"  # no receipt at all

        def refuse(record):
            return {"code": "TEAM_DURABLE_RESULT_UNVERIFIED",
                    "message": "no exact receipt"}

        payload = self.gated(driver, repo_name="idle-early", verify=refuse)
        # Never falsely complete, and never UNKNOWN on an early idle.
        self.assertEqual(payload["status"], "running")
        self.assertNotIn("pane_close", driver.names())
        self.assertFalse(payload["claim"].get("released"))
        # The provisional terminal provenance is withdrawn: an early idle is
        # not a durable terminal capture.
        record = product_state.read_task(payload["task_id"], self.env)
        self.assertFalse(record["output_observations"][-1]["terminal"])

    def test_ordinary_task_without_a_gate_is_never_completed_by_bare_idle(self) -> None:
        driver = FakeDriver()
        driver.wait_state = "idle"
        driver.read_text = "final answer\n" + self.RECEIPT
        payload = self.gated(driver, repo_name="idle-ungated", verify=None)
        self.assertEqual(payload["status"], "running")
        self.assertNotIn("pane_close", driver.names())
        self.assertFalse(payload["claim"].get("released"))

    def test_done_with_a_refused_result_still_holds_unknown(self) -> None:
        # A DONE report is authoritative terminal: an unverified result is
        # terminal-but-unverified, so it holds UNKNOWN rather than RUNNING.
        driver = FakeDriver()
        driver.wait_state = "done"
        driver.read_text = "no receipt"
        payload = self.gated(
            driver, repo_name="done-refused",
            verify=lambda record: {"code": "TEAM_DURABLE_RESULT_UNVERIFIED"})
        self.assertEqual(payload["status"], "unknown")
        self.assertNotIn("pane_close", driver.names())
        self.assertFalse(payload["claim"].get("released"))

    def test_idle_transport_unavailable_never_completes(self) -> None:
        class Unreadable(FakeDriver):
            def agent_wait(self, target, *, until=(), timeout_ms=None):
                raise product_driver.DriverError("wait transport unavailable")

        driver = Unreadable()
        payload = self.gated(
            driver, repo_name="idle-transport", verify=lambda record: None)
        self.assertEqual(payload["status"], "running")
        self.assertNotIn("pane_close", driver.names())
        self.assertFalse(payload["claim"].get("released"))

    def test_idle_completion_is_idempotent_after_process_exit(self) -> None:
        driver = FakeDriver()
        driver.wait_state = "idle"
        driver.read_text = "answer\n" + self.RECEIPT
        repo = git_repo(self.root, name="idle-idempotent")
        args = dict(driver=driver, repo=str(repo), prompt="Run the task",
                    config=self.config, env=self.env, registry_db=self.db)
        first = product_task.run_task(**args, wait_ms=1000,
                                      verify=lambda record: None)
        self.assertEqual(first["status"], "complete")
        self.assertEqual(driver.names().count("agent_prompt"), 1)
        # The runtime loses the worker (process exit). Re-evaluation stays
        # complete, verifies the owned pane is absent, and never re-prompts.
        driver.drop_agents()
        second = product_task.run_task(**args, wait_ms=1000,
                                       verify=lambda record: None)
        self.assertEqual(second["status"], "complete")
        self.assertEqual(driver.names().count("agent_prompt"), 1)

    def test_idle_post_close_recovery_completes_without_replay(self) -> None:
        class ClosedWait(FakeDriver):
            def agent_wait(self, target, *, until=(), timeout_ms=None):
                if "w1:p1" in self.closed_panes:
                    raise product_driver.DriverNotFound("closed worker")
                return super().agent_wait(
                    target, until=until, timeout_ms=timeout_ms)

        driver = ClosedWait()
        driver.wait_state = "idle"
        driver.read_text = "answer\n" + self.RECEIPT
        repo = git_repo(self.root, name="idle-post-close")
        args = dict(driver=driver, repo=str(repo), prompt="Run the task",
                    config=self.config, env=self.env, registry_db=self.db)
        with patch.object(registry, "release", return_value={
                "ok": False, "errors": ["temporary ledger refusal"]}):
            first = product_task.run_task(**args, wait_ms=1000,
                                          verify=lambda record: None)
        self.assertEqual(first["status"], "running")
        self.assertEqual(first["pane_closed"], "w1:p1")
        second = product_task.run_task(**args, wait_ms=1000,
                                       verify=lambda record: None)
        self.assertEqual(second["status"], "complete")
        self.assertTrue(second["claim"]["released"])
        self.assertEqual(driver.names().count("agent_prompt"), 1)
        self.assertEqual(driver.names().count("pane_close"), 1)


class DurableTerminalReceiptTests(TaskCase):
    """A verified settlement is durable BEFORE the first pane-close attempt.

    A crash between that save and the recorded close must neither lose the
    settled receipt nor strand the open claim: a later run re-proves this
    task's own claim, repository, owned pane and durable terminal result, then
    completes the close and releases exactly once -- never re-prompting.
    """

    RECEIPT = "CAO_TEAM_RESULT: " + ("c" * 16)

    def gate(self):
        receipt = self.RECEIPT

        def verify(record):
            for entry in record.get("output_observations") or []:
                if (isinstance(entry, dict) and entry.get("terminal") is True
                        and receipt in str(entry.get("output") or "")):
                    return None
            return {"code": "TEAM_DURABLE_RESULT_UNVERIFIED",
                    "message": "the durable result is not this task's receipt"}

        return verify

    def start(self, driver, task_id, *, name, verify):
        repo = git_repo(self.root, name=name)
        return dict(driver=driver, repo=str(repo), prompt="Run the task",
                    task_id=task_id, config=self.config, env=self.env,
                    registry_db=self.db, wait_ms=1000, verify=verify)

    def claims(self, task_id):
        conn = registry.connect(db=self.db)
        try:
            return conn.execute(
                "SELECT claim_id, released_at, outcome FROM claims WHERE work_key=?",
                (task_id,)).fetchall()
        finally:
            conn.close()

    def test_crash_within_pane_close_keeps_receipt_and_recovers_once(self):
        class CrashOnClose(FakeDriver):
            def pane_close(self, pane_id):
                self._note("pane_close", pane_id)
                self.closed_panes.add(pane_id)   # the close effect happened
                raise RuntimeError("simulated crash inside pane.close")

        driver = CrashOnClose()
        driver.wait_state = "done"
        driver.read_text = "settled answer\n" + self.RECEIPT
        args = self.start(driver, "durable-close", name="durable-close",
                          verify=self.gate())
        with self.assertRaises(RuntimeError):
            product_task.run_task(**args)

        # The verified receipt is durable even though the close was never
        # recorded and the claim is still open.
        record = product_state.read_task("durable-close", self.env)
        self.assertEqual(record["status"], "running")
        self.assertIsNone(record.get("pane_closed"))
        self.assertEqual(record["terminal_result"]["repo"], args["repo"])
        self.assertEqual(record["terminal_result"]["pane_id"], "w1:p1")
        self.assertEqual(record["terminal_result"]["claim_id"],
                         record["claim"]["claim_id"])
        self.assertTrue(any(entry.get("terminal") is True
                            for entry in record["output_observations"]))
        held = self.claims("durable-close")
        self.assertEqual(len(held), 1)
        self.assertIsNone(held[0]["released_at"])

        # The runtime lost the closed worker. Recovery re-proves the durable
        # receipt and the original claim, closes idempotently (already gone)
        # and releases exactly once, without re-prompting.
        class RestartGone(FakeDriver):
            def agent_wait(self, target, *, until=(), timeout_ms=None):
                raise product_driver.DriverNotFound("closed worker")

        restart = RestartGone()
        restart.closed_panes.add("w1:p1")
        args["driver"] = restart
        second = product_task.run_task(**args)
        self.assertEqual(second["status"], "complete")
        self.assertTrue(second["claim"]["released"])
        self.assertEqual(second["claim"]["claim_id"],
                         record["terminal_result"]["claim_id"])
        self.assertEqual(restart.names().count("agent_prompt"), 0)
        self.assertNotIn("pane_close", restart.names())
        released = self.claims("durable-close")
        self.assertEqual(len(released), 1)
        self.assertIsNotNone(released[0]["released_at"])
        self.assertEqual(released[0]["outcome"], "complete")
        # The original terminal evidence survives the reconciliation.
        final = product_state.read_task("durable-close", self.env)
        self.assertTrue(any(
            entry.get("terminal") is True
            and self.RECEIPT in str(entry.get("output") or "")
            for entry in final["output_observations"]))

    def test_crash_before_pane_close_retains_receipt_and_completes(self):
        class CrashBeforeClose(FakeDriver):
            def pane_close(self, pane_id):
                self._note("pane_close", pane_id)
                raise RuntimeError("simulated crash before pane.close")

        driver = CrashBeforeClose()
        driver.wait_state = "done"
        driver.read_text = "settled answer\n" + self.RECEIPT
        args = self.start(driver, "durable-before", name="durable-before",
                          verify=self.gate())
        with self.assertRaises(RuntimeError):
            product_task.run_task(**args)

        record = product_state.read_task("durable-before", self.env)
        self.assertEqual(record["status"], "running")
        self.assertIsNone(record.get("pane_closed"))
        self.assertEqual(record["terminal_result"]["claim_id"],
                         record["claim"]["claim_id"])
        self.assertTrue(any(entry.get("terminal") is True
                            for entry in record["output_observations"]))
        self.assertIsNone(self.claims("durable-before")[0]["released_at"])

        # A later run of the same task proves the same claim/repository and
        # completes exactly once without a second prompt or start.
        restart = FakeDriver()
        restart.wait_state = "done"
        restart.read_text = "settled answer\n" + self.RECEIPT
        args["driver"] = restart
        second = product_task.run_task(**args)
        self.assertEqual(second["status"], "complete")
        self.assertTrue(second["claim"]["released"])
        self.assertEqual(restart.names().count("agent_prompt"), 0)
        self.assertEqual(restart.names().count("agent_start"), 0)
        rows = self.claims("durable-before")
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["released_at"])

    def test_refused_gate_never_writes_a_durable_disposition(self):
        driver = FakeDriver()
        driver.wait_state = "done"
        driver.read_text = "no receipt here"
        args = self.start(driver, "no-durable-done", name="no-durable-done",
                          verify=self.gate())
        payload = product_task.run_task(**args)
        self.assertEqual(payload["status"], "unknown")
        self.assertFalse(payload["claim"].get("released"))
        self.assertNotIn("pane_close", driver.names())
        record = product_state.read_task("no-durable-done", self.env)
        self.assertIsNone(record.get("terminal_result"))

    def test_early_idle_never_writes_a_durable_disposition(self):
        driver = FakeDriver()
        driver.wait_state = "idle"
        driver.read_text = "no receipt here"
        args = self.start(driver, "no-durable-idle", name="no-durable-idle",
                          verify=self.gate())
        payload = product_task.run_task(**args)
        self.assertEqual(payload["status"], "running")
        self.assertFalse(payload["claim"].get("released"))
        self.assertNotIn("pane_close", driver.names())
        record = product_state.read_task("no-durable-idle", self.env)
        self.assertIsNone(record.get("terminal_result"))
        self.assertFalse(record["output_observations"][-1]["terminal"])

    def test_durable_disposition_never_releases_while_its_pane_is_present(self):
        class CrashOnClose(FakeDriver):
            def pane_close(self, pane_id):
                self._note("pane_close", pane_id)
                raise RuntimeError("crash before the close effect")

        driver = CrashOnClose()
        driver.wait_state = "done"
        driver.read_text = "settled\n" + self.RECEIPT
        args = self.start(driver, "durable-pane-open", name="durable-pane-open",
                          verify=self.gate())
        with self.assertRaises(RuntimeError):
            product_task.run_task(**args)

        # The pane still exists and the worker cannot be read: a bare wait
        # not-found must never release the claim from the durable disposition.
        class UnreadableWait(FakeDriver):
            def agent_wait(self, target, *, until=(), timeout_ms=None):
                raise product_driver.DriverNotFound("worker gone")

        restart = UnreadableWait()          # pane "w1:p1" is still present
        args["driver"] = restart
        payload = product_task.run_task(**args)
        self.assertEqual(payload["status"], "running")
        self.assertFalse(payload["claim"].get("released"))
        self.assertNotIn("pane_close", restart.names())
        self.assertIsNone(self.claims("durable-pane-open")[0]["released_at"])

    def test_foreign_durable_disposition_is_refused(self):
        class CrashOnClose(FakeDriver):
            def pane_close(self, pane_id):
                self._note("pane_close", pane_id)
                self.closed_panes.add(pane_id)   # the close effect happened
                raise RuntimeError("simulated crash inside pane.close")

        driver = CrashOnClose()
        driver.wait_state = "done"
        driver.read_text = "settled\n" + self.RECEIPT
        args = self.start(driver, "durable-foreign", name="durable-foreign",
                          verify=self.gate())
        with self.assertRaises(RuntimeError):
            product_task.run_task(**args)

        # A disposition bound to another claim is not this task's own and must
        # never be adopted as a settlement.
        record = product_state.read_task("durable-foreign", self.env)
        record["terminal_result"]["claim_id"] = "claimed-by-someone-else"
        product_state.write_task(record, self.env)

        class RestartGone(FakeDriver):
            def agent_wait(self, target, *, until=(), timeout_ms=None):
                raise product_driver.DriverNotFound("closed worker")

        restart = RestartGone()
        restart.closed_panes.add("w1:p1")
        args["driver"] = restart
        payload = product_task.run_task(**args)
        self.assertEqual(payload["status"], "running")
        self.assertFalse(payload["claim"].get("released"))
        self.assertNotIn("pane_close", restart.names())
        self.assertIsNone(self.claims("durable-foreign")[0]["released_at"])

    def test_recovery_refuses_when_the_durable_receipt_is_tampered(self):
        class CrashOnClose(FakeDriver):
            def pane_close(self, pane_id):
                self._note("pane_close", pane_id)
                self.closed_panes.add(pane_id)   # the close effect happened
                raise RuntimeError("simulated crash inside pane.close")

        driver = CrashOnClose()
        driver.wait_state = "done"
        driver.read_text = "settled\n" + self.RECEIPT
        args = self.start(driver, "durable-tampered", name="durable-tampered",
                          verify=self.gate())
        with self.assertRaises(RuntimeError):
            product_task.run_task(**args)

        # The retained window no longer carries this task's exact receipt, so
        # the recovery gate refuses and the claim stays open.
        record = product_state.read_task("durable-tampered", self.env)
        record["output_observations"] = [
            dict(entry, output="tampered with no receipt")
            for entry in record["output_observations"]]
        product_state.write_task(record, self.env)

        class RestartGone(FakeDriver):
            def agent_wait(self, target, *, until=(), timeout_ms=None):
                raise product_driver.DriverNotFound("closed worker")

        restart = RestartGone()
        restart.closed_panes.add("w1:p1")
        restart.read_text = "unrelated read"
        args["driver"] = restart
        payload = product_task.run_task(**args)
        self.assertEqual(payload["status"], "unknown")
        self.assertFalse(payload["claim"].get("released"))
        self.assertNotIn("pane_close", restart.names())
        self.assertIsNone(self.claims("durable-tampered")[0]["released_at"])


class HerdrFieldCanaryTests(TaskCase):
    """Read-only canaries over the exact Herdr field shapes CAO consumes.

    Herdr reports a pane's worker KIND in ``agent`` while the UNIQUE agent name
    resolves only through ``agent get``; a live agent also carries a settled
    ``status`` and a ``completion_seq``. The positive canary asserts CAO reads
    those fields as documented and admits a verified focused-idle result; the
    negative canary asserts a pane whose occupant CAO cannot resolve as its own
    has NO closure effect (no pane close, no claim release).
    """

    class LiveHerdr(FakeDriver):
        def pane_get(self, pane_id):
            pane = super().pane_get(pane_id)
            pane.setdefault("agent_status", "idle")
            return pane

        def agent_get(self, target):
            base = super().agent_get(target)
            return dict(base, status="idle", completion_seq=7)

    RECEIPT = "CAO_TEAM_RESULT: " + ("b" * 16)

    def canary_repo(self, name):
        return git_repo(self.root, name=name)

    def test_positive_canary_reads_unique_name_not_pane_kind(self) -> None:
        driver = self.LiveHerdr()
        driver.wait_state = "idle"
        driver.read_text = "reviewed\n" + self.RECEIPT
        repo = self.canary_repo("canary-positive")
        payload = product_task.run_task(
            driver=driver, repo=str(repo), prompt="Run the task",
            config=self.config, env=self.env, registry_db=self.db,
            wait_ms=1000, verify=lambda record: None)
        self.assertEqual(payload["status"], "complete")
        record = product_state.read_task(payload["task_id"], self.env)
        unique = record["checkpoint"]["agent_name"]
        # The pane reports the KIND; CAO must resolve this task's UNIQUE name.
        self.assertNotEqual(unique, "opencode")
        gets = [call for call in driver.calls if call[0] == "agent_get"]
        self.assertTrue(gets)
        self.assertTrue(all(call[1][0] == unique for call in gets))

    def test_negative_canary_foreign_occupant_has_no_closure_effect(self) -> None:
        class Foreign(self.LiveHerdr):
            def agent_get(self, target):
                raise product_driver.DriverNotFound("foreign occupant")

        driver = Foreign()
        driver.wait_state = "idle"
        driver.read_text = "answer\n" + self.RECEIPT
        repo = self.canary_repo("canary-negative")
        payload = product_task.run_task(
            driver=driver, repo=str(repo), prompt="Run the task",
            config=self.config, env=self.env, registry_db=self.db,
            wait_ms=1000, verify=lambda record: None)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["error"]["code"], "TASK_PANE_CLOSE_FAILED")
        self.assertNotIn("pane_close", driver.names())
        self.assertFalse(payload["claim"].get("released"))


class BoundedOutputTests(TaskCase):
    """A bounded capture keeps the completion receipt, not the splash screen."""

    LIMIT = 120
    RECEIPT = "CAO_TEAM_RESULT: 0123456789abcdef"

    def bounded_config(self, limit=LIMIT):
        return product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "model": "m"},
            "task": {"max_output_chars": limit},
        })

    def scrollback(self, lines=40):
        prefix = "".join(f"line {index:04d} of the analysis\n" for index in range(lines))
        return prefix + self.RECEIPT + "\n"

    def test_bound_keeps_head_and_tail_differently(self):
        text = self.scrollback()
        stored, bounds = product_task._bounded(text, self.LIMIT)
        # A head-bound capture would keep the beginning and lose the receipt.
        self.assertIn("line 0000 of the analysis", text[:self.LIMIT])
        self.assertNotIn(self.RECEIPT, text[:self.LIMIT])
        self.assertEqual(stored, text[-self.LIMIT:])
        self.assertTrue(stored.endswith(self.RECEIPT + "\n"))
        self.assertNotIn("line 0000 of the analysis", stored)
        self.assertEqual(bounds["kept"], "tail")

    def test_true_truncation_reports_the_dropped_prefix(self):
        text = self.scrollback()
        stored, bounds = product_task._bounded(text, self.LIMIT)
        self.assertEqual(len(stored), self.LIMIT)
        self.assertEqual(bounds["kept"], "tail")
        self.assertEqual(bounds["limit"], self.LIMIT)
        self.assertEqual(bounds["captured_chars"], self.LIMIT)
        self.assertEqual(bounds["dropped_chars"], len(text) - self.LIMIT)

    def test_unbounded_output_is_reported_complete(self):
        stored, bounds = product_task._bounded("agent output", self.LIMIT)
        self.assertEqual(stored, "agent output")
        self.assertEqual(bounds["kept"], "all")
        self.assertEqual(bounds["dropped_chars"], 0)

    def test_bounded_read_retains_the_terminal_receipt(self):
        driver = FakeDriver()
        driver.read_text = self.scrollback()
        payload = product_task.run_task(
            driver=driver, repo=str(self.repo), prompt="Review this repository",
            config=self.bounded_config(), env=self.env, registry_db=self.db,
            wait_ms=0)
        self.assertEqual(payload["status"], "running")
        self.assertTrue(payload["output"].endswith(self.RECEIPT + "\n"))
        self.assertTrue(payload["output_truncated"])
        self.assertEqual(payload["output_bounds"]["kept"], "tail")
        self.assertEqual(payload["output_bounds"]["dropped_chars"],
                         len(driver.read_text) - self.LIMIT)

    def test_unbounded_read_stores_the_verbatim_output(self):
        payload = product_task.run_task(
            driver=FakeDriver(), repo=str(self.repo),
            prompt="Review this repository", config=self.config, env=self.env,
            registry_db=self.db, wait_ms=0)
        self.assertFalse(payload["output_truncated"])
        self.assertEqual(payload["output"], "agent output")
        self.assertEqual(payload["output_bounds"]["kept"], "all")

    def test_shorter_later_read_never_erases_a_captured_receipt(self):
        first = FakeDriver()
        first.read_text = self.scrollback()
        product_task.run_task(
            driver=first, repo=str(self.repo), prompt="Review this repository",
            config=self.bounded_config(), env=self.env, registry_db=self.db,
            wait_ms=0)
        # A resumed observation of the same task reads a shorter window.
        second = FakeDriver()
        second.read_text = "status bar only\n"
        payload = product_task.run_task(
            driver=second, repo=str(self.repo), prompt="Review this repository",
            config=self.bounded_config(), env=self.env, registry_db=self.db,
            wait_ms=0)
        self.assertEqual(payload["status"], "running")
        self.assertTrue(payload["output"].endswith(self.RECEIPT + "\n"))
        self.assertEqual(payload["output_bounds"]["superseded_read_chars"],
                         len(second.read_text))

    def test_logs_view_limit_keeps_the_terminal_end(self):
        driver = FakeDriver()
        driver.read_text = self.scrollback()
        payload = product_task.run_task(
            driver=driver, repo=str(self.repo), prompt="Review this repository",
            config=self.bounded_config(), env=self.env, registry_db=self.db,
            wait_ms=0)
        logs = product_task.read_logs(
            payload["task_id"], env=self.env, limit=len(self.RECEIPT) + 1)
        self.assertEqual(logs["output"], self.RECEIPT + "\n")
        self.assertTrue(logs["truncated"])
        bounds = logs["output_bounds"]
        self.assertEqual(bounds["kept"], "tail")
        self.assertEqual(bounds["view_dropped_chars"],
                         self.LIMIT - len(self.RECEIPT) - 1)

    def test_logs_of_an_unbounded_output_report_no_truncation(self):
        payload = product_task.run_task(
            driver=FakeDriver(), repo=str(self.repo),
            prompt="Review this repository", config=self.config, env=self.env,
            registry_db=self.db, wait_ms=0)
        logs = product_task.read_logs(payload["task_id"], env=self.env)
        self.assertEqual(logs["output"], "agent output")
        self.assertFalse(logs["truncated"])
        self.assertEqual(logs["output_bounds"]["dropped_chars"], 0)


class ObservationRetentionTests(TaskCase):
    """A task retains both directions of a scrolling pane, with provenance.

    Generic capture deliberately cannot know the task's completion nonce, so it
    never guesses which of two windows proves completion. An earlier longer read
    that missed the receipt and a later shorter read that carries it must both
    survive, and the converse must not lose a receipt already proved.
    """

    LIMIT = 120
    RECEIPT = "CAO_TEAM_RESULT: 0123456789abcdef"

    def setUp(self) -> None:
        super().setUp()
        self.defaults = product_config.task_defaults(product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "model": "m"},
            "task": {"max_output_chars": self.LIMIT},
        }))

    def record(self, **checkpoint) -> dict:
        base = {"agent_name": "cao-agent", "prompt_issued": True,
                "prompt_ack": "accepted", "stage": "prompted"}
        base.update(checkpoint)
        return {
            "schema": product_state.TASK_SCHEMA, "task_id": "obs-task",
            "output": None, "output_truncated": False, "output_bounds": None,
            "output_observations": [], "checkpoint": base,
        }

    def observe(self, record, text, *, terminal=False):
        driver = FakeDriver()
        driver.read_text = text
        product_task._observe(driver, record, self.env, None, self.defaults,
                              terminal=terminal)
        return record

    @staticmethod
    def scrollback(lines=40, tail=""):
        return "".join(
            f"line {index:04d} of the analysis\n" for index in range(lines)
        ) + tail

    def test_durable_capture_requests_unwrapped_source(self):
        """A post-prompt read must not soft-wrap an exact SHA verdict line.

        The durable read asks the runtime for the unwrapped window so a
        verdict line wider than the pane survives verbatim in the durable
        observation instead of being broken across soft-wrap lines.
        """
        line = "CAO_REVIEWED_HEAD: " + "a" * 40  # exactly 59 characters
        self.assertEqual(len(line), 59)

        class RecordingSourceDriver(FakeDriver):
            def agent_read(self, target, *, lines=200, source="recent"):
                self._note("agent_read", target, source)
                return self.read_text

        driver = RecordingSourceDriver()
        driver.read_text = line + "\n"
        record = self.record()
        product_task._observe(driver, record, self.env, None, self.defaults,
                              terminal=True)
        sources = [call[1][1] for call in driver.calls
                   if call[0] == "agent_read"]
        self.assertEqual(sources, ["recent-unwrapped"])
        self.assertIn(line, record["output"])
        self.assertIn(line, record["output_observations"][-1]["output"])

    def test_later_shorter_receipt_is_retained_alongside_the_longer_window(self):
        long_read = self.scrollback(lines=40)
        short_read = "analysis complete\n" + self.RECEIPT + "\n"
        record = self.record()
        self.observe(record, long_read)
        self.observe(record, short_read, terminal=True)
        # The longer receipt-free read stays the primary capture ...
        self.assertNotIn(self.RECEIPT, record["output"])
        self.assertEqual(len(record["output"]), self.LIMIT)
        # ... but the shorter read that carries the receipt is not discarded.
        texts = [entry["output"] for entry in record["output_observations"]]
        self.assertIn(record["output"], texts)
        self.assertIn(short_read, texts)
        short = next(entry for entry in record["output_observations"]
                     if entry["output"] == short_read)
        self.assertTrue(short["post_prompt"])
        self.assertEqual(short["prompt_ack"], "accepted")
        self.assertEqual(short["stage"], "prompted")
        self.assertTrue(short["terminal"])
        self.assertEqual(short["bounds"]["kept"], "all")
        self.assertEqual(short["bounds"]["dropped_chars"], 0)
        self.assertIsInstance(short["observed_at"], str)

    def test_earlier_receipt_is_not_lost_when_a_longer_read_arrives(self):
        short_read = "done\n" + self.RECEIPT + "\n"
        long_read = self.scrollback(lines=40)
        record = self.record()
        self.observe(record, short_read, terminal=True)
        self.observe(record, long_read)
        # The longer later read becomes primary, but the proved receipt stays.
        self.assertNotIn(self.RECEIPT, record["output"])
        texts = [entry["output"] for entry in record["output_observations"]]
        self.assertIn(short_read, texts)

    def test_observations_are_bounded_and_the_longest_survives(self):
        record = self.record()
        self.observe(record, "L" * 400)
        longest = record["output"]
        self.assertEqual(len(longest), self.LIMIT)
        for index in range(10):
            self.observe(record, "short read " + str(index))
        windows = record["output_observations"]
        self.assertLessEqual(len(windows), product_task._MAX_OBSERVATIONS)
        for entry in windows:
            self.assertLessEqual(len(entry["output"]), self.LIMIT)
            self.assertIsInstance(entry["observed_at"], str)
        self.assertIn(longest, [entry["output"] for entry in windows])

    def test_receipt_retention_does_not_duplicate_a_prompt_or_claim(self):
        first = FakeDriver()
        first.read_text = self.scrollback(lines=40)
        product_task.run_task(
            driver=first, repo=str(self.repo), prompt="Review this repository",
            config=product_config.validate_config({
                "schema": product_config.SCHEMA,
                "provider": {"cli": "opencode", "model": "m"},
                "task": {"max_output_chars": self.LIMIT},
            }), env=self.env, registry_db=self.db, wait_ms=0)
        second = FakeDriver()
        second.read_text = "analysis complete\n" + self.RECEIPT + "\n"
        product_task.run_task(
            driver=second, repo=str(self.repo), prompt="Review this repository",
            config=product_config.validate_config({
                "schema": product_config.SCHEMA,
                "provider": {"cli": "opencode", "model": "m"},
                "task": {"max_output_chars": self.LIMIT},
            }), env=self.env, registry_db=self.db, wait_ms=0)
        # The exact task resumes: one start, one prompt, one claim, and the
        # second observation never re-issues the prompt.
        self.assertEqual(first.names().count("agent_start"), 1)
        self.assertEqual(first.names().count("agent_prompt"), 1)
        self.assertEqual(second.names().count("agent_start"), 0)
        self.assertEqual(second.names().count("agent_prompt"), 0)
        conn = registry.connect(db=self.db)
        try:
            claims = conn.execute("SELECT claim_id FROM claims").fetchall()
        finally:
            conn.close()
        self.assertEqual(len(claims), 1)


class ClaimAcquisitionCrashTests(TaskCase):
    """A crash between the durable claim and the record save must be provable.

    The acquisition intent is written before the claim, so recovery can only
    adopt the row when the checkout is still exactly the recorded precrash
    baseline. Dirty, drifted or unattributable evidence is refused instead.
    """

    def setUp(self) -> None:
        super().setUp()
        self.write_repo = git_repo(self.root, name="crash-scope", files=("a.txt",))
        remote = self.root / "upstream.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                       capture_output=True, env={**os.environ, **GIT_ENV})
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "--set-upstream", "origin", "main")
        self.driver = FakeDriver()

    def git(self, *arguments: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.write_repo), "-c", "commit.gpgsign=false", *arguments],
            capture_output=True, text=True, check=True,
            env={**os.environ, **GIT_ENV},
        )
        return proc.stdout.strip()

    def write_kwargs(self, driver=None, **overrides):
        kwargs = dict(
            driver=driver or self.driver, repo=str(self.write_repo),
            prompt="Edit a.txt", task_id="crash-case", mode="write",
            write_scope=("a.txt",), observation=RuntimeObservation.empty(),
            config=self.config, env=self.env, registry_db=self.db,
        )
        kwargs.update(overrides)
        return kwargs

    def claims(self):
        conn = registry.connect(db=self.db)
        try:
            return conn.execute(
                "SELECT claim_id, mode, branch, worktree, released_at, outcome, "
                "released_head FROM claims ORDER BY claim_id").fetchall()
        finally:
            conn.close()

    def crash_after_the_durable_claim(self):
        """Die in the exact window this finding is about."""
        real = registry.claim

        def crashing(conn, spec, observation=None, persist_run=None):
            real(conn, spec, observation, persist_run)
            raise RuntimeError("crash between the durable claim and the record save")

        with patch.object(registry, "claim", crashing):
            with self.assertRaises(RuntimeError):
                product_task.run_task(**self.write_kwargs(), wait_ms=0)
        held = self.claims()
        self.assertEqual(len(held), 1)
        self.assertIsNone(held[0]["released_at"])
        # The record on disk never saw the claim, but it did see the intent.
        record = product_state.read_task("crash-case", self.env)
        self.assertIsNone(record["claim"])
        self.assertEqual(record["status"], "pending")
        self.assertEqual(record["claim_intent"]["base_head"], self.git("rev-parse", "HEAD"))
        return held[0]["claim_id"]

    def assert_refused_and_still_claimed(self, needle):
        payload = product_task.run_task(**self.write_kwargs(), wait_ms=0)
        self.assertEqual(payload["status"], "refused")
        self.assertEqual(payload["refusal"]["status"], registry.NEEDS_RECONCILIATION)
        self.assertIn(needle, " ".join(payload["refusal"]["errors"]))
        held = self.claims()
        # Never a second claim, never an invalid release, never a silent stall.
        self.assertEqual(len(held), 1)
        self.assertIsNone(held[0]["released_at"])
        self.assertEqual(self.driver.names().count("agent_start"), 0)
        return held[0]["claim_id"]

    def test_crash_in_the_claim_window_recovers_the_same_claim_and_exact_release(self):
        claim_id = self.crash_after_the_durable_claim()
        resumed = product_task.run_task(**self.write_kwargs(), wait_ms=0)
        self.assertEqual(resumed["status"], "running")
        self.assertEqual(resumed["claim"]["claim_id"], claim_id)
        self.assertEqual(len(self.claims()), 1)
        (self.write_repo / "a.txt").write_text("committed\n")
        self.git("add", "a.txt")
        self.git("commit", "-m", "task output")
        final = product_task.run_task(**self.write_kwargs(), wait_ms=1000)
        self.assertEqual(final["status"], "complete")
        held = self.claims()
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["claim_id"], claim_id)
        self.assertEqual(held[0]["outcome"], "complete")
        self.assertEqual(held[0]["released_head"], self.git("rev-parse", "HEAD"))
        self.assertEqual(final["claim"]["released_head"], self.git("rev-parse", "HEAD"))

    def test_crash_recovery_refuses_an_unprovable_dirty_checkout(self):
        self.crash_after_the_durable_claim()
        (self.write_repo / "a.txt").write_text("uncommitted work\n")
        self.assert_refused_and_still_claimed("dirty after claim acquisition")

    def test_crash_recovery_refuses_a_post_acquisition_commit(self):
        self.crash_after_the_durable_claim()
        (self.write_repo / "other.txt").write_text("drifted\n")
        self.git("add", "other.txt")
        self.git("commit", "-m", "work after the claim")
        self.assert_refused_and_still_claimed("precrash baseline")

    def test_crash_recovery_refuses_a_tampered_or_mismatched_intent(self):
        self.crash_after_the_durable_claim()
        record = product_state.read_task("crash-case", self.env)
        record["claim_intent"]["scope"] = ["b.txt"]
        product_state.write_task(record, self.env)
        self.assert_refused_and_still_claimed(
            "the recorded claim intent is not this task's own")

    def test_crash_recovery_refuses_a_claim_row_that_is_not_this_task(self):
        claim_id = self.crash_after_the_durable_claim()
        conn = registry.connect(db=self.db)
        try:
            conn.execute("UPDATE claims SET mode='READ' WHERE claim_id=?", (claim_id,))
        finally:
            conn.close()
        self.assert_refused_and_still_claimed("mode is not this task's mode")

    def test_claim_without_a_provable_intent_is_still_refused(self):
        # A row this task cannot attribute to a recorded intent keeps the
        # original fail-closed refusal instead of being adopted.
        held = self.claims()
        self.assertEqual(held, [])
        record = product_state.read_task("crash-case", self.env)
        self.assertIsNone(record)
        conn = registry.connect(db=self.db)
        try:
            registry.propose(conn, "cao-product", "crash-case", str(self.write_repo),
                             paths=["a.txt"], objective="foreign")
            registry.claim(conn, registry.ClaimSpec(
                run_id="cao-product", work_key="crash-case",
                owner=product_task.OWNER, mode=registry.WRITE, issue="",
                branch="main", worktree=str(self.write_repo)),
                RuntimeObservation.empty())
        finally:
            conn.close()
        payload = product_task.run_task(**self.write_kwargs(), wait_ms=0)
        self.assertEqual(payload["status"], "refused")
        self.assertTrue(payload["refusal"]["errors"])
        held = self.claims()
        self.assertEqual(len(held), 1)
        self.assertIsNone(held[0]["released_at"])
        self.assertEqual(held[0]["mode"], "WRITE")
        self.assertEqual(self.driver.names().count("agent_start"), 0)

    def test_read_crash_in_the_claim_window_recovers_its_own_read_claim(self):
        driver = FakeDriver()
        real = registry.claim

        def crashing(conn, spec, observation=None, persist_run=None):
            real(conn, spec, observation, persist_run)
            raise RuntimeError("crash between the durable claim and the record save")

        kwargs = dict(driver=driver, repo=str(self.repo), prompt="Read this repository",
                      task_id="read-crash", observation=RuntimeObservation.empty(),
                      config=self.config, env=self.env, registry_db=self.db)
        with patch.object(registry, "claim", crashing):
            with self.assertRaises(RuntimeError):
                product_task.run_task(**kwargs, wait_ms=0)
        held = self.claims()
        self.assertEqual([row["mode"] for row in held], ["READ"])
        resumed = product_task.run_task(**kwargs, wait_ms=0)
        self.assertEqual(resumed["status"], "running")
        self.assertEqual(resumed["claim"]["claim_id"], held[0]["claim_id"])
        self.assertEqual(len(self.claims()), 1)


class TeamModelPolicyTests(TaskCase):
    """Optional operator-local allowlist: absent is unchanged, configured denies."""

    def policy_config(self, allowlist):
        return product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "model": "opencode/step-5-preview-free"},
            "team_model_allowlist": allowlist,
        })

    def test_absent_allowlist_preserves_existing_behavior(self):
        driver = FakeDriver()
        payload = product_task.run_task(
            driver=driver, repo=str(self.repo),
            prompt="Review this repository", task_id="policy-absent",
            config=self.config, env=self.env, registry_db=self.db, wait_ms=0)
        self.assertEqual(payload["status"], "running")
        self.assertEqual(driver.names().count("agent_start"), 1)

    def test_explicit_empty_or_null_allowlist_is_refused_closed(self):
        # A present value is never an opt-out: only the ABSENCE of the field
        # is. An explicit [] or null must be refused at validate_config, so a
        # half-written strict gate can never be silently disabled.
        for bad in ([], None):
            with self.subTest(allowlist=bad):
                with self.assertRaises(product_config.ProductConfigError):
                    self.policy_config(bad)

    def test_configured_routes_admit_effort_variants_in_resolved_argv(self):
        # Every configured allowlist route is honored, and an #effort
        # variant carried in the ACTUAL resolved provider argv is the same
        # base route: the gate reads the verified argv selection, normalizes
        # it, and dispatches the real variant.
        routes = ("opencode/step-5-preview-free",
                  "opencode-go/glm-5.3-flash",
                  "opencode-go/deepseek-v4.1-flash")
        for index, route in enumerate(routes):
            for variant in ("#high", "#xhigh"):
                with self.subTest(route=route, variant=variant):
                    config = product_config.validate_config({
                        "schema": product_config.SCHEMA,
                        "provider": {"cli": "opencode", "model": route},
                        "task": {"agent_args": [
                            "mini", "--standalone",
                            "--model", route + variant]},
                        "team_model_allowlist": list(routes),
                    })
                    driver = FakeDriver()
                    payload = product_task.run_task(
                        driver=driver, repo=str(self.repo),
                        prompt="Review this repository",
                        task_id=f"policy-{index}-{variant[1:]}", config=config,
                        env=self.env, registry_db=self.db, wait_ms=0)
                    self.assertEqual(payload["status"], "running")
                    self.assertEqual(driver.names().count("agent_start"), 1)
                    self.assertEqual(driver.names().count("agent_prompt"), 1)
                    # The dispatched argv really carried the effort variant.
                    start = [call for call in driver.calls
                             if call[0] == "agent_start"][0]
                    self.assertIn(route + variant, start[1][3])

    def test_unlisted_route_is_refused_with_zero_claims(self):
        # Any unlisted route — including another model on the same
        # opencode-go provider — is denied before the first effect.
        config = self.policy_config([
            "opencode/step-5-preview-free",
            "opencode-go/glm-5.3-flash",
            "opencode-go/deepseek-v4.1-flash",
        ])
        for route in ("opencode-go/example-unlisted-a",
                      "opencode-go/example-unlisted-b"):
            with self.subTest(route=route):
                denied = product_config.validate_config({
                    "schema": product_config.SCHEMA,
                    "provider": {"cli": "opencode", "model": route},
                    "team_model_allowlist": ["opencode/step-5-preview-free"],
                })
                driver = FakeDriver()
                payload = product_task.run_task(
                    driver=driver, repo=str(self.repo),
                    prompt="Review this repository",
                    config=denied, env=self.env, registry_db=self.db,
                    wait_ms=0)
                self.assertEqual(payload["status"], "refused")
                self.assertEqual(payload["refusal"]["stage"], "model_policy")
                self.assertEqual(payload["refusal"]["errors"],
                                 ["TEAM_MODEL_NOT_ALLOWED"])
                self.assertEqual(driver.names(), [])
                self.assertIsNone(payload["claim"])
                self.assertEqual(self.claim_rows(), [])

    def test_disallowed_model_is_refused_before_any_agent_effect(self):
        config = self.policy_config(["opencode-go/glm-5.3-flash"])
        driver = FakeDriver()
        payload = product_task.run_task(
            driver=driver, repo=str(self.repo), prompt="Review this repository",
            config=config, env=self.env, registry_db=self.db, wait_ms=0)
        self.assertEqual(payload["status"], "refused")
        self.assertEqual(payload["refusal"]["stage"], "model_policy")
        self.assertEqual(payload["refusal"]["errors"], ["TEAM_MODEL_NOT_ALLOWED"])
        self.assertEqual(driver.names().count("agent_start"), 0)
        self.assertEqual(driver.names().count("agent_prompt"), 0)
        # The refusal precedes every governed effect: the record holds no
        # claim at all, so the ledger has zero rows for this task and no
        # workspace or pane was ever created.
        self.assertIsNone(payload["claim"])
        self.assertIsNone(payload["workspace"])
        self.assertIsNone(payload["pane_id"])
        self.assertIsNone(payload["agent"])
        self.assertEqual(self.claim_rows(), [])
        # The refusal is not a silent state: with no claim to release and no
        # pane identity to verify, the supported stop path still ends the
        # task cleanly.
        stopped = product_task.stop_task(
            driver=driver, task_id=payload["task_id"], env=self.env,
            registry_db=self.db)
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(self.claim_rows(), [])

    def test_allowed_model_dispatches(self):
        config = self.policy_config(["opencode/step-5-preview-free"])
        driver = FakeDriver()
        payload = product_task.run_task(
            driver=driver, repo=str(self.repo), prompt="Review this repository",
            config=config, env=self.env, registry_db=self.db, wait_ms=0)
        self.assertEqual(payload["status"], "running")
        self.assertEqual(driver.names().count("agent_start"), 1)

    def test_step5_write_task_is_refused_with_zero_effects(self):
        # The canonical policy restricts Step-5 Preview Free to READ: a direct
        # WRITE task bound to it is refused before any Registry claim, leaving
        # zero claim rows and no workspace, pane or agent effect.
        write_repo = git_repo(self.root, name="step5-write", files=("a.txt",))
        config = self.policy_config(["opencode/step-5-preview-free"])
        driver = FakeDriver()
        payload = product_task.run_task(
            driver=driver, repo=str(write_repo), prompt="Edit a.txt",
            mode="write", write_scope=("a.txt",), config=config, env=self.env,
            registry_db=self.db, wait_ms=0)
        self.assertEqual(payload["status"], "refused")
        self.assertEqual(payload["refusal"]["stage"], "model_policy")
        self.assertEqual(payload["refusal"]["errors"], ["TEAM_ROUTE_READ_ONLY"])
        self.assertEqual(driver.names(), [])
        self.assertIsNone(payload["claim"])
        self.assertEqual(self.claim_rows(), [])

    def test_step5_write_effort_variant_is_refused(self):
        # A bounded #effort variant never changes the base route, so it cannot
        # restore write access to the READ-only Step-5 Preview Free route.
        write_repo = git_repo(self.root, name="step5-effort",
                              files=("a.txt",))
        selected = "opencode/step-5-preview-free#high"
        config = product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "model": selected},
            "task": {"agent_args": ["mini", "--standalone",
                                    "--model", selected]},
            "team_model_allowlist": ["opencode/step-5-preview-free"],
        })
        driver = FakeDriver()
        payload = product_task.run_task(
            driver=driver, repo=str(write_repo), prompt="Edit a.txt",
            mode="write", write_scope=("a.txt",), config=config, env=self.env,
            registry_db=self.db, wait_ms=0)
        self.assertEqual(payload["status"], "refused")
        self.assertEqual(payload["refusal"]["errors"], ["TEAM_ROUTE_READ_ONLY"])
        self.assertEqual(driver.names(), [])
        self.assertEqual(self.claim_rows(), [])

    def test_effort_suffix_is_accepted_as_an_effort_variant(self):
        # A fleet configured at base effort admits the same base id carrying an
        # accepted #effort variant; the variant is never a different route.
        policy = product_config.team_model_policy(product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "model": "opencode/step-5-preview-free"},
            "team_model_allowlist": ["opencode/step-5-preview-free"],
        }))
        self.assertTrue(policy.permits("opencode/step-5-preview-free"))
        self.assertTrue(policy.permits("opencode/step-5-preview-free#high"))
        self.assertTrue(policy.permits("opencode/step-5-preview-free#xhigh"))
        self.assertFalse(policy.permits("opencode/step-5-preview-free-2#high"))

    def test_unresolvable_argv_model_is_refused_closed(self):
        # A divergent provider/argv selection is ambiguous, never a guess.
        config = product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "claude", "model": "claude/aaa"},
            "task": {"agent_args": ["--model", "bbb"]},
            "team_model_allowlist": ["claude/aaa"],
        })
        driver = FakeDriver()
        payload = product_task.run_task(
            driver=driver, repo=str(self.repo), prompt="Review this repository",
            config=config, env=self.env, registry_db=self.db, wait_ms=0)
        self.assertEqual(payload["status"], "refused")
        self.assertEqual(payload["refusal"]["errors"], ["TEAM_MODEL_UNRESOLVED"])
        self.assertEqual(driver.names().count("agent_start"), 0)

    def test_started_task_is_not_re_gated_by_a_later_policy_change(self):
        allowed = self.policy_config(["opencode/step-5-preview-free"])
        driver = FakeDriver()
        first = product_task.run_task(
            driver=driver, repo=str(self.repo), prompt="Review this repository",
            task_id="policy-resume", config=allowed, env=self.env,
            registry_db=self.db, wait_ms=0)
        self.assertEqual(first["status"], "running")
        # Once an agent is started and prompted, a later allowlist change never
        # re-gates the existing execution: no second start and no second prompt.
        denied = self.policy_config(["opencode-go/glm-5.3-flash"])
        again = product_task.run_task(
            driver=driver, repo=str(self.repo), prompt="Review this repository",
            task_id="policy-resume", config=denied, env=self.env,
            registry_db=self.db, wait_ms=0)
        self.assertEqual(again["status"], "running")
        self.assertEqual(driver.names().count("agent_start"), 1)
        self.assertEqual(driver.names().count("agent_prompt"), 1)


class OpenCodeMiniWithoutAllowlistTests(TaskCase):
    """The mini-harness gate also covers direct NEW tasks with no allowlist.

    An absent ``team_model_allowlist`` must never let a direct OpenCode task
    launch the bare interactive TUI or an implicit model. Every refusal below
    happens before any Registry claim, workspace, pane or agent effect, while a
    valid explicit mini harness and a non-OpenCode route keep their behavior.
    """

    def make_config(self, provider_model=None, args=None, cli="opencode"):
        provider = {"cli": cli}
        if provider_model is not None:
            provider["model"] = provider_model
        payload = {"schema": product_config.SCHEMA, "provider": provider}
        if args is not None:
            payload["task"] = {"agent_args": list(args)}
        return product_config.validate_config(payload)

    def dispatch(self, config, task_id):
        driver = FakeDriver()
        payload = product_task.run_task(
            driver=driver, repo=str(self.repo), task_id=task_id,
            prompt="Review this repository", config=config, env=self.env,
            registry_db=self.db, wait_ms=0)
        return driver, payload

    def assert_refused(self, driver, payload, reason):
        self.assertEqual(payload["status"], "refused")
        self.assertEqual(payload["refusal"]["stage"], "model_policy")
        self.assertEqual(payload["refusal"]["errors"], [reason])
        self.assertEqual(driver.names(), [])
        self.assertIsNone(payload["claim"])
        self.assertIsNone(payload["workspace"])
        self.assertIsNone(payload["pane_id"])
        self.assertIsNone(payload["agent"])
        self.assertEqual(self.claim_rows(), [])

    def test_bare_or_unknown_opencode_launch_is_refused_with_zero_effects(self):
        for index, args in enumerate((
                [], ["--model", "m"], ["--standalone", "--model", "m"],
                ["mini", "--model", "m", "--print"],
                ["mini", "--agent", "build", "--model", "m"])):
            with self.subTest(args=args):
                driver, payload = self.dispatch(
                    self.make_config(args=args), f"mini-tui-{index}")
                # A bare launch with no model at all is unresolved; an explicit
                # model on a non-mini/unknown argv is the TUI refusal.
                reason = ("TEAM_MODEL_UNRESOLVED" if not args
                          else "TEAM_ROUTE_TUI_UNQUALIFIED")
                self.assert_refused(driver, payload, reason)

    def test_missing_duplicate_or_unsafe_model_is_refused_with_zero_effects(self):
        for index, args in enumerate((
                ["mini"], ["mini", "--model"],
                ["mini", "--model="],
                ["mini", "--model", "m", "--model", "m"],
                ["mini", "--model", "not a safe token"])):
            with self.subTest(args=args):
                driver, payload = self.dispatch(
                    self.make_config(args=args), f"mini-model-{index}")
                self.assert_refused(driver, payload, "TEAM_MODEL_UNRESOLVED")

    def test_explicit_mini_harness_without_allowlist_dispatches(self):
        for index, args in enumerate((
                ["mini", "--model", "m"],
                ["mini", "--standalone", "--model", "m"])):
            with self.subTest(args=args):
                driver, payload = self.dispatch(
                    self.make_config(args=args), f"mini-valid-{index}")
                self.assertEqual(payload["status"], "running")
                self.assertEqual(driver.names().count("agent_start"), 1)
                self.assertEqual(driver.names().count("agent_prompt"), 1)
                start = next(call for call in driver.calls
                             if call[0] == "agent_start")
                self.assertEqual(start[1][3], tuple(args))

    def test_non_opencode_route_without_allowlist_is_unaffected(self):
        for index, (kind, cli) in enumerate(
                (("claude", "claude"), ("codex", "codex"))):
            with self.subTest(kind=kind):
                args = ["--model", "sonnet" if kind == "claude"
                        else "gpt-6.1-sol"]
                driver, payload = self.dispatch(
                    self.make_config(cli=cli, args=args), f"non-opencode-{index}")
                self.assertEqual(payload["status"], "running")
                self.assertEqual(driver.names().count("agent_start"), 1)
                start = next(call for call in driver.calls
                             if call[0] == "agent_start")
                self.assertEqual(start[1][1], kind)

    def test_issued_claim_and_prompt_are_resumed_without_re_gating(self):
        valid = self.make_config(args=["mini", "--model", "m"])
        driver = FakeDriver()
        first = product_task.run_task(
            driver=driver, repo=str(self.repo), task_id="mini-resume",
            prompt="Review this repository", config=valid, env=self.env,
            registry_db=self.db, wait_ms=0)
        self.assertEqual(first["status"], "running")
        # The same execution is later observed under an invalid bare route.
        # Its original claim and prompt are already issued, so it resumes
        # exactly: no second start, no second prompt, no replacement claim.
        again = product_task.run_task(
            driver=driver, repo=str(self.repo), task_id="mini-resume",
            prompt="Review this repository", config=self.make_config(),
            env=self.env, registry_db=self.db, wait_ms=0)
        self.assertEqual(again["status"], "running")
        self.assertEqual(again["claim"]["claim_id"], first["claim"]["claim_id"])
        self.assertEqual(driver.names().count("agent_start"), 1)
        self.assertEqual(driver.names().count("agent_prompt"), 1)


class TeamRouteIdentityTests(TaskCase):
    """An allowlisted OpenCode model id binds the actual OpenCode runtime.

    A model string alone never proved OpenCode routing or a paid provider:
    a route with ``agent_kind="claude"`` and ``provider.cli="claude"`` used to
    pass an allowlist of OpenCode model ids and dispatch on the foreign
    runtime. A configured allowlist now refuses a foreign, missing or spoofed
    runtime identity, and every direct refusal below leaves zero Registry
    claim rows and no workspace, pane or agent effect.
    """

    CONFIGURED_ROUTES = ("opencode/step-5-preview-free",
                         "opencode-go/glm-5.3-flash",
                         "opencode-go/deepseek-v4.1-flash")

    def identity_config(self, provider, task=None):
        return product_config.validate_config({
            "schema": product_config.SCHEMA, "provider": provider,
            "task": task or {},
            "team_model_allowlist": list(self.CONFIGURED_ROUTES),
        })

    def refused_dispatch(self, config):
        driver = FakeDriver()
        payload = product_task.run_task(
            driver=driver, repo=str(self.repo), prompt="Review this repository",
            config=config, env=self.env, registry_db=self.db, wait_ms=0)
        return driver, payload

    def assert_zero_effects(self, driver, payload):
        self.assertEqual(payload["status"], "refused")
        self.assertEqual(payload["refusal"]["stage"], "model_policy")
        self.assertEqual(payload["refusal"]["errors"],
                         ["TEAM_ROUTE_IDENTITY_MISMATCH"])
        self.assertEqual(driver.names(), [])
        self.assertIsNone(payload["claim"])
        self.assertIsNone(payload["workspace"])
        self.assertIsNone(payload["pane_id"])
        self.assertIsNone(payload["agent"])
        self.assertEqual(self.claim_rows(), [])

    def test_spoofed_claude_provider_cli_is_refused(self):
        # A fake synthetic route that claims agent_kind=opencode while the
        # provider CLI dispatches claude is refused, even though the provider
        # model and the verified argv model both look allowed.
        config = self.identity_config(
            {"cli": "claude", "model": "opencode-go/glm-5.3-flash"},
            {"agent_kind": "opencode",
             "agent_args": ["mini", "--standalone",
                            "--model", "opencode-go/glm-5.3-flash"]})
        self.assert_zero_effects(*self.refused_dispatch(config))

    def test_foreign_agent_kind_is_refused(self):
        # kind=claude with the actual CLI never proves OpenCode routing.
        config = self.identity_config(
            {"cli": "claude", "model": "opencode-go/glm-5.3-flash"},
            {"agent_kind": "claude"})
        self.assert_zero_effects(*self.refused_dispatch(config))

    def test_missing_provider_cli_is_refused(self):
        # A listing model id without a provider CLI names no runtime at all.
        config = self.identity_config(
            {"model": "opencode/step-5-preview-free"},
            {"agent_kind": "opencode"})
        self.assert_zero_effects(*self.refused_dispatch(config))

    def test_missing_agent_kind_is_refused_at_the_shared_policy(self):
        # The direct task runner's agent-kind fallback can never produce a
        # missing kind, so the same shared policy refusal is pinned on the
        # route shape any caller supplies.
        policy = product_config.team_model_policy(self.identity_config(
            {"cli": "opencode", "model": "opencode-go/glm-5.3-flash"}))
        self.assertEqual(product_config.model_policy_error(
            {"provider": {"cli": "opencode",
                          "model": "opencode-go/glm-5.3-flash"},
             "agent_args": []}, policy),
            "TEAM_ROUTE_IDENTITY_MISMATCH")

    def test_refused_write_task_leaves_zero_claim_rows(self):
        # A direct refused WRITE task must also predate the first Registry
        # claim: zero claim rows and no runtime effect of any kind.
        write_repo = git_repo(self.root, name="identity-write", files=("a.txt",))
        config = self.identity_config(
            {"cli": "claude", "model": "opencode-go/glm-5.3-flash"})
        driver = FakeDriver()
        payload = product_task.run_task(
            driver=driver, repo=str(write_repo), prompt="Edit a.txt",
            mode="write", write_scope=("a.txt",), config=config, env=self.env,
            registry_db=self.db, wait_ms=0)
        self.assert_zero_effects(driver, payload)

    def test_actual_opencode_routes_still_dispatch_with_effort_variants(self):
        # The configured routes dispatched by the real OpenCode identity (kind
        # and CLI both opencode) keep working, including each #effort variant
        # carried by the resolved provider argv.
        for index, route in enumerate(self.CONFIGURED_ROUTES):
            for variant in ("", "#high", "#xhigh"):
                with self.subTest(route=route, variant=variant):
                    config = self.identity_config(
                        {"cli": "opencode", "model": route + variant})
                    driver = FakeDriver()
                    payload = product_task.run_task(
                        driver=driver, repo=str(self.repo),
                        prompt="Review this repository",
                        task_id=f"identity-{index}-{variant[1:] or 'base'}",
                        config=config, env=self.env, registry_db=self.db,
                        wait_ms=0)
                    self.assertEqual(payload["status"], "running")
                    self.assertEqual(driver.names().count("agent_start"), 1)


class DirectCodexAndNonZdrModeTests(TaskCase):
    """The direct task runner enforces the same route and mode decision."""

    def codex_config(self):
        return product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "codex"},
            "task": {"agent_args": ["--model", "gpt-6.1-sol"]},
            "team_model_allowlist": ["codex/gpt-6.1-sol"],
        })

    def test_read_codex_route_dispatches(self):
        driver = FakeDriver()
        payload = product_task.run_task(
            driver=driver, repo=str(self.repo), prompt="Review this repository",
            task_id="codex-read", config=self.codex_config(), env=self.env,
            registry_db=self.db, wait_ms=0)
        self.assertEqual(payload["status"], "running")
        self.assertEqual(driver.names().count("agent_start"), 1)

    def test_unqualified_codex_write_route_has_zero_effects(self):
        driver = FakeDriver()
        payload = product_task.run_task(
            driver=driver, repo=str(self.repo), prompt="Edit a.txt",
            task_id="codex-write", mode="write", write_scope=("a.txt",),
            config=self.codex_config(), env=self.env, registry_db=self.db,
            wait_ms=0)
        self.assertEqual(payload["status"], "refused")
        self.assertEqual(payload["refusal"]["errors"], ["TEAM_ROUTE_WRITE_UNQUALIFIED"])
        self.assertEqual(driver.names(), [])
        self.assertEqual(self.claim_rows(), [])

    def test_non_zdr_route_is_refused_even_when_allowlisted(self):
        route = "opencode-go/gemini-4-argon-contributor"
        config = product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "model": route},
            "team_model_allowlist": [route],
        })
        driver = FakeDriver()
        payload = product_task.run_task(
            driver=driver, repo=str(self.repo), prompt="Review this repository",
            task_id="gemini-read", config=config, env=self.env,
            registry_db=self.db, wait_ms=0)
        self.assertEqual(payload["status"], "refused")
        self.assertEqual(payload["refusal"]["errors"],
                         ["TEAM_ROUTE_PRIVACY_UNVERIFIED"])
        self.assertEqual(driver.names(), [])
        self.assertEqual(self.claim_rows(), [])


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
