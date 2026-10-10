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
    """Record calls and answer with deterministic runtime-shaped values."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.workspaces: list[dict] = []
        self.prompt_exc: Exception | None = None
        self.wait_state = "done"
        self.read_text = "agent output"
        self.agent_present = False
        self.closed_panes: set[str] = set()

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
        if pane_id in self.closed_panes:
            raise product_driver.DriverNotFound("closed pane")
        return {"pane_id": pane_id, "workspace_id": "w1"}

    def pane_close(self, pane_id):
        self._note("pane_close", pane_id)
        self.closed_panes.add(pane_id)
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
