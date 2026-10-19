"""Hermetic tests for the standalone disposition control.

The fixtures use synthetic repositories, synthetic worktrees and a real SQLite
Work Registry created by the existing module. Every protected grant lives in a
temporary directory and uses the current test UID; no real system path, host
configuration, credential or production Registry is touched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from cao import (
    disposition_control as dc,
    intake_authority,
    registry,
    team,
)

_HAVE_GIT = shutil.which("git") is not None


def _git(cwd, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.stdout.decode("utf-8", "replace").strip()


def _make_repo(root: Path) -> tuple[Path, Path]:
    origin = root / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Synthetic Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "main")
    _git(repo, "remote", "set-head", "origin", "main")
    return repo, origin


def _add_conflicts(root: Path, repo: Path) -> dict[str, Path]:
    other = root / "wt-other"
    _git(repo, "worktree", "add", "-b", "other", str(other), "main")
    (other / "src" / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "feature")

    dirty = root / "wt-dirty"
    _git(repo, "worktree", "add", "-b", "dirty", str(dirty), "main")
    (dirty / "README.md").write_text("dirty one\n", encoding="utf-8")

    detached = root / "wt-detached"
    _git(repo, "worktree", "add", "--detach", str(detached), "main")
    (detached / "src" / "detached.txt").write_text("detached\n", encoding="utf-8")
    _git(detached, "add", "-A")
    _git(detached, "commit", "-m", "detached")

    quiet = root / "wt-quiet"
    _git(repo, "worktree", "add", "-b", "quiet", str(quiet), "main")
    return {"other": other, "dirty": dirty, "detached": detached, "quiet": quiet}


def _spec(repo: Path, team_id: str = "squad") -> dict:
    return {
        "schema": team.SPEC_SCHEMA,
        "team_id": team_id,
        "roles": {
            "developer": {"instruction": "Implement precise edits"},
            "analyst": {"instruction": "Read the repository"},
        },
        "tasks": [
            {
                "id": "implementation",
                "role": "developer",
                "repo": str(repo),
                "prompt": "Implement the bounded change",
                "mode": "write",
                "write_scope": ["README.md", "src"],
            }
        ],
        "max_writers": 1,
        "max_parallel": 1,
    }


class DispositionCase(unittest.TestCase):
    def setUp(self) -> None:
        if not _HAVE_GIT:
            self.skipTest("git is required")
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.env = {
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
        }
        self.db = str(self.root / "registry.sqlite3")
        registry.connect(self.db).close()
        self.repo, self.origin = _make_repo(self.root)
        team.initialize(_spec(self.repo), env=self.env)

        executable = self.root / "synthetic-runtime"
        executable.touch()
        dc.product_state.write_managed({"path": str(executable)}, env=self.env)
        self.runtime = mock.Mock()
        self.runtime.workspace_list.return_value = []

        self.protected = self.root / "protected"
        self.protected.mkdir(mode=0o755)
        os.chmod(self.protected, 0o755)
        self.grant_path = self.protected / dc.GRANT_BASENAME
        self._patches = [
            mock.patch.dict(os.environ, {**self.env, "CAO_REGISTRY_DB": self.db}),
            mock.patch.object(dc.product_driver, "RuntimeDriver",
                              return_value=self.runtime),
            mock.patch.object(dc, "_GRANT_DIRECTORY", str(self.protected)),
            mock.patch.object(dc, "_GRANT_BASENAME", dc.GRANT_BASENAME),
            mock.patch.object(dc, "_GRANT_UID", os.getuid()),
            mock.patch.object(dc, "_TRUSTED_BASE", str(self.root)),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    # -- fixtures ---------------------------------------------------------

    def observation(self) -> registry.RuntimeObservation:
        return registry.RuntimeObservation.empty()

    def plan(self, **overrides) -> dict:
        kwargs = {
            "env": self.env,
            "observation": self.observation(),
            "db": self.db,
        }
        kwargs.update(overrides)
        return dc.plan("squad", **kwargs)

    def add_conflicts(self) -> dict[str, Path]:
        return _add_conflicts(self.root, self.repo)

    def grant_document(self, plan: dict, *, pairs=None, **overrides) -> dict:
        moment = datetime.now(timezone.utc).replace(microsecond=0)
        if pairs is None:
            pairs = [item for item in plan["conflicts"]
                     if item["claim_id"] is None and not item["owner"]]
        document = {
            "schema": dc.GRANT_SCHEMA,
            "team_id": plan["team_id"],
            "plan_digest": plan["plan_digest"],
            "manifest_digest": plan["manifest_digest"],
            "operator": "synthetic-operator",
            "issued_at": (moment - timedelta(minutes=1)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "expires_at": (moment + timedelta(hours=1)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "revoked": False,
            "action": dc.ACTION,
            "scope": {"repo": plan["repo"]["path"],
                      "paths": list(plan["scope"])},
            "worktrees": [
                {"worktree": item["worktree"],
                 "fingerprint": item["fingerprint"]}
                for item in pairs
            ],
        }
        document.update(overrides)
        return document

    def write_grant(self, document, mode: int = 0o600) -> None:
        if isinstance(document, (dict, list)):
            text = json.dumps(document)
        else:
            text = str(document)
        self.grant_path.write_text(text, encoding="utf-8")
        os.chmod(self.grant_path, mode)

    def authorize(self, **overrides) -> dict:
        kwargs = {
            "env": self.env,
            "observation": self.observation(),
            "db": self.db,
        }
        kwargs.update(overrides)
        return dc.authorize("squad", **kwargs)

    def apply(self, **overrides) -> dict:
        return dc.apply("squad", **overrides)

    def occupy_runtime(self, worktree: Path) -> None:
        self.runtime.workspace_list.return_value = [{"workspace_id": "ws"}]
        self.runtime.pane_list.return_value = [{"pane_id": "pane"}]
        self.runtime.pane_get.return_value = {
            "agent": "synthetic-agent", "cwd": str(worktree),
            "agent_status": "running",
        }

    def verify(self, **overrides) -> dict:
        kwargs = {
            "env": self.env,
            "observation": self.observation(),
            "db": self.db,
        }
        kwargs.update(overrides)
        return dc.verify("squad", **kwargs)

    def read_conn(self):
        return registry.connect_readonly(self.db)

    def counts(self) -> tuple[int, int]:
        conn = self.read_conn()
        try:
            claims = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
            dispositions = conn.execute(
                "SELECT COUNT(*) FROM dispositions").fetchone()[0]
        finally:
            conn.close()
        return claims, dispositions

    def claim_writer(self) -> None:
        """Create a real open WRITE claim while the checkout is still clean."""

        conn = registry.connect(self.db)
        try:
            registry.propose(conn, "cao-team-squad", "existing", str(self.repo),
                             paths=["README.md"])
            result = registry.claim(
                conn,
                registry.ClaimSpec(
                    run_id="cao-team-squad", work_key="existing",
                    owner="cao-product-task", mode=registry.WRITE,
                    branch="main", worktree=str(self.repo)),
                registry.RuntimeObservation.empty(),
            )
            self.assertTrue(result.get("ok"), result)
        finally:
            conn.close()


class PlanTests(DispositionCase):
    def test_plan_shows_every_conflict_and_is_read_only(self) -> None:
        conflicts = self.add_conflicts()
        before = self.counts()
        team_bytes = Path(team.team_path("squad", self.env)).read_bytes()

        payload = self.plan()

        self.assertEqual(payload["status"], "PLAN")
        self.assertEqual(payload["run_id"], "cao-team-squad")
        self.assertEqual(len(payload["manifest_digest"]), 64)
        self.assertEqual(len(payload["plan_digest"]), 64)
        paths = {item["worktree"] for item in payload["conflicts"]}
        self.assertEqual(paths, {os.path.realpath(path)
                                 for path in conflicts.values()
                                 if path.name != "wt-quiet"})
        by_name = {Path(item["worktree"]).name: item
                   for item in payload["conflicts"]}
        self.assertTrue(by_name["wt-detached"]["detached"])
        self.assertTrue(by_name["wt-dirty"]["dirty_digest"])
        self.assertEqual(by_name["wt-other"]["kind"],
                         registry.DISCOVERED_UNREGISTERED)
        self.assertEqual(payload["preflight"]["status"], "CONFLICT")
        self.assertEqual(payload["active_claims"], [])
        for item in payload["conflicts"]:
            self.assertEqual(registry.finding_fingerprint(item),
                             item["fingerprint"])
        self.assertEqual(self.counts(), before)
        self.assertEqual(Path(team.team_path("squad", self.env)).read_bytes(),
                         team_bytes)

    def test_plan_digest_is_stable_for_identical_live_state(self) -> None:
        self.add_conflicts()
        first = self.plan()
        second = self.plan()
        self.assertEqual(first["plan_digest"], second["plan_digest"])

    def test_plan_requires_a_paused_safe_team(self) -> None:
        team.set_desired("squad", "running", env=self.env)
        with self.assertRaises(dc.DispositionRefused):
            self.plan()

    def test_plan_requires_a_registered_team(self) -> None:
        with self.assertRaises(dc.DispositionRefused):
            dc.plan("absent", env=self.env, observation=self.observation(),
                    db=self.db)

    def test_plan_rejects_missing_runtime(self) -> None:
        self.add_conflicts()
        Path(dc.product_state.managed_record_path(self.env)).unlink()
        with self.assertRaises(dc.DispositionUnavailable):
            dc.plan("squad", env=self.env, observation=None, db=self.db)

    def test_plan_rejects_unreadable_occupancy(self) -> None:
        self.add_conflicts()
        with self.assertRaises(dc.DispositionUnavailable):
            self.plan(observation=registry.RuntimeObservation.unavailable("x"))

    def test_plan_rejects_unknown_agent_state(self) -> None:
        conflicts = self.add_conflicts()
        observation = registry.RuntimeObservation(
            True,
            (registry.AgentObservation(
                "agent-one", str(conflicts["other"]), "not-a-state"),),
            "",
        )
        with self.assertRaises(dc.DispositionRefused):
            self.plan(observation=observation)


class AuthorityTests(DispositionCase):
    def test_missing_grant_is_unavailable(self) -> None:
        self.add_conflicts()
        with self.assertRaises(dc.DispositionUnavailable):
            self.authorize()

    def test_valid_grant_authorizes_without_writing(self) -> None:
        self.add_conflicts()
        plan = self.plan()
        self.write_grant(self.grant_document(plan))
        before = self.counts()
        result = self.authorize()
        self.assertEqual(result["status"], "AUTHORIZED")
        self.assertEqual(result["operator"], "synthetic-operator")
        self.assertEqual(len(result["approved"]), 3)
        self.assertEqual(len(result["binding_digest"]), 64)
        self.assertEqual(self.counts(), before)

    def test_expired_and_revoked_grants_are_refused(self) -> None:
        self.add_conflicts()
        plan = self.plan()
        expired = self.grant_document(plan)
        expired["issued_at"] = "2020-01-01T00:00:00Z"
        expired["expires_at"] = "2020-01-01T01:00:00Z"
        self.write_grant(expired)
        with self.assertRaises(dc.DispositionRefused):
            self.authorize()

        revoked = self.grant_document(plan, revoked=True)
        self.write_grant(revoked)
        with self.assertRaises(dc.DispositionRefused):
            self.authorize()

    def test_stale_plan_and_wrong_manifest_are_refused(self) -> None:
        self.add_conflicts()
        plan = self.plan()
        self.write_grant(self.grant_document(plan, plan_digest="a" * 64))
        with self.assertRaises(dc.DispositionRefused):
            self.authorize()

        self.write_grant(self.grant_document(plan, manifest_digest="b" * 64))
        with self.assertRaises(dc.DispositionRefused):
            self.authorize()

    def test_off_scope_and_unapproved_pairs_are_refused(self) -> None:
        self.add_conflicts()
        plan = self.plan()
        wrong_scope = self.grant_document(plan)
        wrong_scope["scope"] = {"repo": plan["repo"]["path"],
                                "paths": ["README.md"]}
        self.write_grant(wrong_scope)
        with self.assertRaises(dc.DispositionRefused):
            self.authorize()

        injected = self.grant_document(plan)
        injected["worktrees"].append({
            "worktree": plan["repo"]["path"],
            "fingerprint": "f" * 64,
        })
        self.write_grant(injected)
        with self.assertRaises(dc.DispositionRefused):
            self.authorize()

    def test_wrong_fingerprint_action_and_operator_are_refused(self) -> None:
        self.add_conflicts()
        plan = self.plan()
        wrong = self.grant_document(plan)
        wrong["worktrees"][0]["fingerprint"] = "c" * 64
        self.write_grant(wrong)
        with self.assertRaises(dc.DispositionRefused):
            self.authorize()

        for field, value in (("action", "historical"),
                             ("operator", "../bad"),
                             ("revoked", "false")):
            document = self.grant_document(plan)
            document[field] = value
            self.write_grant(document)
            with self.assertRaises(dc.DispositionRefused):
                self.authorize()

    def test_duplicate_keys_are_refused(self) -> None:
        self.add_conflicts()
        plan = self.plan()
        document = self.grant_document(plan)
        text = json.dumps(document)
        duplicated = text[:-1] + ', "team_id": "squad"}'
        self.write_grant(duplicated)
        with self.assertRaises(dc.DispositionRefused):
            self.authorize()

    def test_reader_refuses_wrong_owner_mode_symlink_and_shape(self) -> None:
        self.add_conflicts()
        plan = self.plan()
        valid = self.grant_document(plan)

        self.write_grant(valid)
        with mock.patch.object(dc, "_GRANT_UID", 0):
            with self.assertRaises(dc.DispositionRefused):
                dc._load_protected_grant()

        self.write_grant(valid, mode=0o666)
        with self.assertRaises(dc.DispositionRefused):
            dc._load_protected_grant()

        target = self.root / "elsewhere.json"
        target.write_text(json.dumps(valid), encoding="utf-8")
        os.chmod(target, 0o600)
        os.remove(self.grant_path)
        os.symlink(target, self.grant_path)
        with self.assertRaises(dc.DispositionRefused):
            dc._load_protected_grant()

        os.remove(self.grant_path)
        link_directory = self.root / "linked-protected"
        os.symlink(self.protected, link_directory)
        with mock.patch.object(dc, "_GRANT_DIRECTORY", str(link_directory)):
            self.write_grant(valid)
            with self.assertRaises(dc.DispositionRefused):
                dc._load_protected_grant()

    def test_reader_refuses_malformed_oversized_and_non_regular(self) -> None:
        self.add_conflicts()
        plan = self.plan()
        valid = self.grant_document(plan)

        self.write_grant("{not json")
        with self.assertRaises(dc.DispositionRefused):
            dc._load_protected_grant()

        self.grant_path.write_bytes(b"{" + b" " * (dc.MAX_GRANT_BYTES + 1))
        os.chmod(self.grant_path, 0o600)
        with self.assertRaises(dc.DispositionRefused):
            dc._load_protected_grant()

        self.grant_path.unlink()
        self.grant_path.mkdir()
        with self.assertRaises(dc.DispositionRefused):
            dc._load_protected_grant()

        self.grant_path.rmdir()
        self.write_grant(valid)

    def test_falsified_document_is_refused(self) -> None:
        self.add_conflicts()
        plan = self.plan()
        document = self.grant_document(plan)
        document["extra"] = True
        with self.assertRaises(dc.DispositionRefused):
            dc.validate_grant(document)
        document = self.grant_document(plan)
        document["scope"]["paths"] = ["../escape"]
        with self.assertRaises(dc.DispositionRefused):
            dc.validate_grant(document)
        document = self.grant_document(plan)
        document["worktrees"][0]["worktree"] = "*"
        with self.assertRaises(dc.DispositionRefused):
            dc.validate_grant(document)
        document = self.grant_document(plan)
        document["granted_at"] = document["issued_at"]
        with self.assertRaises(dc.DispositionRefused):
            dc.validate_grant(document)
        legacy = self.grant_document(plan)
        legacy["granted_at"] = legacy.pop("issued_at")
        self.assertEqual(dc.validate_grant(legacy)["team_id"], "squad")


class ApplyTests(DispositionCase):
    def test_apply_is_atomic_idempotent_and_claim_neutral(self) -> None:
        self.add_conflicts()
        plan = self.plan()
        self.write_grant(self.grant_document(plan))
        before_claims, before_dispositions = self.counts()
        team_bytes = Path(team.team_path("squad", self.env)).read_bytes()

        result = self.apply()
        self.assertEqual(result["status"], "APPLIED")
        self.assertEqual(result["inserted"], 3)
        self.assertEqual(result["claims_changed"], 0)
        claims, dispositions = self.counts()
        self.assertEqual(claims, before_claims)
        self.assertEqual(dispositions, before_dispositions + 3)
        self.assertEqual(Path(team.team_path("squad", self.env)).read_bytes(),
                         team_bytes)

        replay = self.apply()
        self.assertEqual(replay["status"], "REPLAYED")
        self.assertEqual(replay["inserted"], 0)
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.counts(), (claims, dispositions))

        conn = self.read_conn()
        try:
            rows = conn.execute(
                "SELECT disposition, verified_historical, authority, rationale "
                "FROM dispositions ORDER BY disp_id").fetchall()
        finally:
            conn.close()
        self.assertEqual({row["disposition"] for row in rows}, {"independent"})
        self.assertTrue(all(row["verified_historical"] == 0 for row in rows))
        self.assertTrue(all("synthetic-operator" in row["authority"]
                            for row in rows))
        self.assertTrue(all("plan_digest=" in row["rationale"]
                            for row in rows))

    def test_apply_refuses_changed_dirty_digest(self) -> None:
        conflicts = self.add_conflicts()
        plan = self.plan()
        self.write_grant(self.grant_document(plan))
        (conflicts["dirty"] / "README.md").write_text(
            "dirty changed again\n", encoding="utf-8")
        changed = self.plan()
        document = self.grant_document(plan)
        document["plan_digest"] = changed["plan_digest"]
        self.write_grant(document)
        with self.assertRaises(dc.DispositionRefused):
            self.apply()
        self.assertEqual(self.counts()[1], 0)

    def test_apply_refuses_moved_head(self) -> None:
        conflicts = self.add_conflicts()
        plan = self.plan()
        self.write_grant(self.grant_document(plan))
        (conflicts["other"] / "src" / "feature.txt").write_text(
            "moved\n", encoding="utf-8")
        _git(conflicts["other"], "add", "-A")
        _git(conflicts["other"], "commit", "-m", "moved")
        changed = self.plan()
        document = self.grant_document(plan)
        document["plan_digest"] = changed["plan_digest"]
        self.write_grant(document)
        with self.assertRaises(dc.DispositionRefused):
            self.apply()
        self.assertEqual(self.counts()[1], 0)

    def test_apply_refuses_moved_repository_head(self) -> None:
        self.add_conflicts()
        plan = self.plan()
        self.write_grant(self.grant_document(plan))
        _git(self.repo, "commit", "--allow-empty", "-m", "moved base")
        with self.assertRaises(dc.DispositionRefused):
            self.apply()
        self.assertEqual(self.counts()[1], 0)

    def test_apply_never_creates_a_missing_registry(self) -> None:
        self.add_conflicts()
        plan = self.plan()
        self.write_grant(self.grant_document(plan))
        os.remove(self.db)
        with self.assertRaises(dc.DispositionUnavailable):
            self.apply()
        self.assertFalse(os.path.exists(self.db))

    def test_apply_refuses_active_writer_claim(self) -> None:
        self.claim_writer()
        self.add_conflicts()
        plan = self.plan()
        self.assertTrue(plan["active_claims"])
        self.write_grant(self.grant_document(plan))
        self.authorize()
        with self.assertRaises(dc.DispositionRefused):
            self.apply()
        self.assertEqual(self.counts()[1], 0)

    def test_apply_refuses_expired_or_revoked_grant(self) -> None:
        self.add_conflicts()
        plan = self.plan()
        expired = self.grant_document(plan)
        expired["issued_at"] = "2020-01-01T00:00:00Z"
        expired["expires_at"] = "2020-01-01T01:00:00Z"
        self.write_grant(expired)
        with self.assertRaises(dc.DispositionRefused):
            self.apply()

        self.write_grant(self.grant_document(plan, revoked=True))
        with self.assertRaises(dc.DispositionRefused):
            self.apply()
        self.assertEqual(self.counts()[1], 0)

    def test_mid_batch_error_rolls_everything_back(self) -> None:
        self.add_conflicts()
        plan = self.plan()
        self.write_grant(self.grant_document(plan))
        real = registry.record_observation
        calls = {"count": 0}

        def flaky(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("synthetic mid-batch failure")
            return real(*args, **kwargs)

        with mock.patch.object(registry, "record_observation", flaky):
            with self.assertRaises(RuntimeError):
                self.apply()
        self.assertEqual(calls["count"], 2)
        self.assertEqual(self.counts()[1], 0)

    def test_apply_rejects_clock_override_for_expired_grant(self) -> None:
        self.add_conflicts()
        plan = self.plan()
        self.write_grant(self.grant_document(
            plan, issued_at="2020-01-01T00:00:00Z",
            expires_at="2020-01-01T01:00:00Z"))
        with self.assertRaises(TypeError):
            self.apply(now="2020-01-01T00:30:00Z")
        with self.assertRaisesRegex(dc.DispositionRefused, "expired"):
            self.apply()
        self.assertEqual(self.counts(), (0, 0))

    def test_apply_rejects_observation_override_hiding_live_agent(self) -> None:
        conflicts = self.add_conflicts()
        self.write_grant(self.grant_document(self.plan()))
        self.occupy_runtime(conflicts["other"])
        with self.assertRaises(TypeError):
            self.apply(observation=registry.RuntimeObservation.empty())
        with self.assertRaises(dc.DispositionRefused):
            self.apply()
        self.runtime.workspace_list.assert_called()
        self.assertEqual(self.counts(), (0, 0))

    def test_apply_rejects_registry_overrides_hiding_writer_claim(self) -> None:
        self.claim_writer()
        self.add_conflicts()
        shadow = str(self.root / "shadow.sqlite3")
        registry.connect(shadow).close()
        self.write_grant(self.grant_document(self.plan(db=shadow)))
        before = self.counts()
        for override in ({"db": shadow},
                         {"env": {**self.env, "CAO_REGISTRY_DB": shadow}}):
            with self.subTest(override=override):
                with self.assertRaises(TypeError):
                    self.apply(**override)
                self.assertEqual(self.counts(), before)
                conn = registry.connect_readonly(shadow)
                try:
                    self.assertEqual(conn.execute(
                        "SELECT COUNT(*) FROM dispositions").fetchone()[0], 0)
                finally:
                    conn.close()
        with self.assertRaises(dc.DispositionRefused):
            self.apply()

    def test_apply_rejects_environment_override_hiding_live_runtime(self) -> None:
        conflicts = self.add_conflicts()
        self.write_grant(self.grant_document(self.plan()))
        shadow_env = {**self.env,
                      "XDG_STATE_HOME": str(self.root / "shadow-state"),
                      "CAO_REGISTRY_DB": self.db}
        source = Path(team.team_path("squad", self.env))
        target = Path(team.team_path("squad", shadow_env))
        target.parent.mkdir(parents=True)
        shutil.copyfile(source, target)
        executable = self.root / "shadow-runtime"
        executable.touch()
        dc.product_state.write_managed({"path": str(executable)}, env=shadow_env)
        empty_runtime = mock.Mock()
        empty_runtime.workspace_list.return_value = []
        self.occupy_runtime(conflicts["other"])

        def driver(*, executable):
            return (empty_runtime if executable == str(self.root / "shadow-runtime")
                    else self.runtime)

        with mock.patch.object(dc.product_driver, "RuntimeDriver",
                               side_effect=driver):
            with self.assertRaises(TypeError):
                self.apply(env=shadow_env)
            with self.assertRaises(dc.DispositionRefused):
                self.apply()
        self.assertEqual(self.counts(), (0, 0))

    def test_apply_rejects_even_none_overrides(self) -> None:
        for name in ("now", "observation", "env", "db"):
            with self.subTest(name=name):
                with self.assertRaises(TypeError):
                    self.apply(**{name: None})
        self.assertEqual(self.counts(), (0, 0))

    def test_apply_rechecks_expiry_under_registry_lock(self) -> None:
        self.add_conflicts()
        document = self.grant_document(self.plan())
        self.write_grant(document)
        real_clock = dc._utc_now
        before = real_clock(document["issued_at"]) + timedelta(seconds=1)
        expired = real_clock(document["expires_at"])
        ticks = iter((before, expired))

        def clock(value=None):
            return real_clock(value) if value is not None else next(ticks)

        with mock.patch.object(dc, "_utc_now", side_effect=clock):
            with self.assertRaisesRegex(dc.DispositionRefused, "expired"):
                self.apply()
        self.assertEqual(self.counts(), (0, 0))

    def test_apply_rolls_back_if_grant_expires_during_batch(self) -> None:
        self.add_conflicts()
        document = self.grant_document(self.plan())
        self.write_grant(document)
        real_clock = dc._utc_now
        before = real_clock(document["issued_at"]) + timedelta(seconds=1)
        expired = real_clock(document["expires_at"])
        moment = before
        record = registry.record_observation

        def clock(value=None):
            return real_clock(value) if value is not None else moment

        def expire(*args, **kwargs):
            nonlocal moment
            result = record(*args, **kwargs)
            moment = expired
            return result

        with mock.patch.object(dc, "_utc_now", side_effect=clock), \
                mock.patch.object(registry, "record_observation", side_effect=expire):
            with self.assertRaisesRegex(dc.DispositionRefused, "expired"):
                self.apply()
        self.assertEqual(self.counts(), (0, 0))

    def test_apply_rolls_back_if_grant_changes_before_batch_or_commit(self) -> None:
        self.add_conflicts()
        document = self.grant_document(self.plan())
        for target in ("_build_plan", "_insert_independent"):
            with self.subTest(target=target):
                self.write_grant(document)
                original = getattr(dc, target)

                def replace(*args, **kwargs):
                    result = original(*args, **kwargs)
                    self.write_grant({**document, "operator": "other-operator"})
                    return result

                with mock.patch.object(dc, target, side_effect=replace):
                    with self.assertRaisesRegex(dc.DispositionRefused,
                                                "grant changed"):
                        self.apply()
                self.assertEqual(self.counts(), (0, 0))

    def test_apply_probes_runtime_again_under_registry_lock(self) -> None:
        conflicts = self.add_conflicts()
        self.write_grant(self.grant_document(self.plan()))
        self.occupy_runtime(conflicts["other"])
        self.runtime.workspace_list.side_effect = [[], [{"workspace_id": "ws"}]]
        with self.assertRaises(dc.DispositionRefused):
            self.apply()
        self.assertEqual(self.runtime.workspace_list.call_count, 2)
        self.assertEqual(self.counts(), (0, 0))

    def test_apply_refuses_live_agent_even_with_matching_grant(self) -> None:
        conflicts = self.add_conflicts()
        self.occupy_runtime(conflicts["other"])
        self.write_grant(self.grant_document(self.plan(observation=None)))
        with self.assertRaisesRegex(dc.DispositionRefused, "live runtime agent"):
            self.apply()
        self.assertEqual(self.counts(), (0, 0))

    def test_apply_refuses_unavailable_runtime_before_or_under_lock(self) -> None:
        self.add_conflicts()
        self.write_grant(self.grant_document(self.plan()))
        for readings in ([RuntimeError("unreadable")],
                         [[], RuntimeError("unreadable")]):
            with self.subTest(readings=len(readings)):
                self.runtime.workspace_list.side_effect = readings
                with self.assertRaises(dc.DispositionUnavailable):
                    self.apply()
                self.assertEqual(self.counts(), (0, 0))

    def test_independent_disposition_is_not_reused_by_another_run(self) -> None:
        self.add_conflicts()
        plan = self.plan()
        self.write_grant(self.grant_document(plan))
        self.apply()
        finding = plan["conflicts"][0]
        conn = self.read_conn()
        try:
            own = registry.find_disposition(conn, plan["run_id"], finding)
            other = registry.find_disposition(conn, "cao-team-other", finding)
        finally:
            conn.close()
        self.assertIsNotNone(own)
        self.assertIsNone(other)


class VerifyTests(DispositionCase):
    def test_verify_reports_remaining_then_resolved_without_side_effects(self) -> None:
        self.add_conflicts()
        plan = self.plan()
        before = self.counts()
        team_bytes = Path(team.team_path("squad", self.env)).read_bytes()

        pending = self.verify()
        self.assertFalse(pending["ready_for_separate_writer_dispatch"])
        self.assertEqual(len(pending["remaining"]), 3)
        self.assertEqual(pending["preflight"]["status"], "CONFLICT")
        self.assertFalse(pending["auto_started"])
        self.assertFalse(pending["claim_written"])
        self.assertEqual(self.counts(), before)

        self.write_grant(self.grant_document(plan))
        self.apply()
        resolved = self.verify()
        self.assertTrue(resolved["ready_for_separate_writer_dispatch"])
        self.assertEqual(resolved["remaining"], [])
        self.assertEqual(len(resolved["resolved"]), 3)
        self.assertTrue(all(item["disposition"] == "independent"
                            for item in resolved["resolved"]))
        self.assertEqual(resolved["preflight"]["status"], "PREFLIGHT")
        self.assertEqual(self.counts(), (before[0], before[1] + 3))
        self.assertEqual(Path(team.team_path("squad", self.env)).read_bytes(),
                         team_bytes)


class LegacySurfaceTests(unittest.TestCase):
    """The six accepted legacy concerns stay exactly where they were."""

    def setUp(self) -> None:
        if not _HAVE_GIT:
            self.skipTest("git is required")
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.env = {"XDG_STATE_HOME": str(self.root / "state"),
                    "XDG_CONFIG_HOME": str(self.root / "config")}
        self.db = str(self.root / "legacy.sqlite3")
        self.conn = registry.connect(self.db)

    def tearDown(self) -> None:
        self.conn.close()

    def test_registry_historical_contract_is_unchanged(self) -> None:
        finding = {
            "kind": registry.STALE_CLEAN,
            "status": "claimed",
            "worktree": str(self.root / "wt"),
            "head": "a" * 40,
            "branch": "main",
            "worktree_branch": "main",
            "claim_id": 7,
            "owner": "owner",
            "scope": ["src"],
            "components": [],
        }
        result = registry.record_disposition(
            self.conn, "legacy-run", finding, registry.DISPOSITION_JOIN,
            "operator-attribution", "legacy rationale")
        self.assertFalse(result["verified_historical"])
        self.assertTrue(callable(registry.record_historical_disposition))

    def test_product_cli_does_not_gain_disposition_verbs(self) -> None:
        from cao import product_cli

        parser = product_cli.build_parser()
        choices = set()
        for action in parser._actions:
            if hasattr(action, "choices") and action.choices:
                choices.update(action.choices)
        self.assertNotIn("disposition", choices)
        self.assertIn("team", choices)

    def test_team_owner_contract_is_unchanged(self) -> None:
        repo = self.root / "team-repo"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.email", "test@example.invalid")
        _git(repo, "config", "user.name", "Synthetic Test")
        (repo / "README.md").write_text("x\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "x")
        created = team.initialize(_spec(repo, "legacy-team"), env=self.env)
        self.assertEqual(created["status"], "PAUSED")
        self.assertEqual(team.status("legacy-team", env=self.env)["status"],
                         "PAUSED")

    def test_intake_authority_remains_a_distinct_grant(self) -> None:
        self.assertTrue(callable(intake_authority.read_protected_grant))
        self.assertEqual(intake_authority.DEFAULT_GRANT_DIR, "/etc/cao")
        self.assertEqual(intake_authority.DEFAULT_GRANT_BASENAME,
                         "intake-authority.json")
        self.assertNotEqual(intake_authority.DEFAULT_GRANT_BASENAME,
                            dc.GRANT_BASENAME)

    def test_team_execution_document_is_unchanged(self) -> None:
        path = Path(__file__).resolve().parents[1] / "docs" / "team-execution.md"
        text = path.read_text(encoding="utf-8")
        self.assertIn("fingerprint-bound historical-disposition contract", text)
        self.assertIn("no second ledger", text)
        self.assertNotIn("cao-disposition", text)

    def test_no_historical_disposition_is_recorded_by_this_command(self) -> None:
        self.assertNotIn(
            "record_historical_disposition",
            Path(dc.__file__).read_text(encoding="utf-8"))
        self.assertEqual(registry.DISPOSITION_HISTORICAL, "historical")
        self.assertEqual(registry.DISPOSITION_INDEPENDENT, "independent")


if __name__ == "__main__":
    unittest.main()
