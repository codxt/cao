"""Synthetic security tests for the B7 durable Intake execution queue.

Covers: enqueue of a prequalified, source-bound READ intent and the refusal of
everything else; the forge/replay and tamper/corrupt-store fail-closed paths; a
stale source HEAD; missed, unmet and cyclic dependencies; duplicate execution
and crash-restart idempotency; concurrent passes; the absence of any authority
(CLI, environment or JSON) and of any model/privacy qualification; the
self-blocking isolation of a held item and priority fairness; a bounded
per-tick budget; the auto-advance of an already-approved immutable READ team
and the refusal of a forged approval; the daemon opt-in (default disabled); and
that a held item mutates no team and no Registry claim.

Nothing here is shipped and no real payload is used. Every value is synthetic.
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cao import (intake, intake_handoff, intake_queue, intake_triage,
                 product_config, product_daemon, product_state, state, team)

_HAVE_GIT = shutil.which("git") is not None


def run_git(cwd, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return proc.stdout.decode("utf-8", "replace")


def make_repo(root: Path, name: str = "repo") -> Path:
    path = root / name
    path.mkdir(parents=True)
    run_git(path, "init", "-b", "main")
    run_git(path, "config", "user.email", "test@example.invalid")
    run_git(path, "config", "user.name", "Test")
    run_git(path, "config", "commit.gpgsign", "false")
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    run_git(path, "add", "-A")
    run_git(path, "commit", "-m", "init")
    return path


def commit_all(repo: Path, message: str = "update") -> str:
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-m", message)
    return run_git(repo, "rev-parse", "HEAD").strip()


_READ_REQUEST = (
    "Read-only audit of the scoped source. Revalidate the default branch and "
    "report evidence. Never merge, deploy, publish or change credentials."
)


class QueueCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.env = {
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
        }
        self.state_root = Path(self.env["XDG_STATE_HOME"]) / "cao"
        self.repo = make_repo(self.root)

    # -- fixtures ------------------------------------------------------------
    def submit(self, item_id: str, body: str) -> str:
        document = {"schema": intake.REQUEST_SCHEMA, "item_id": item_id,
                    "body": body, "title": "synthetic", "source": "operator"}
        return intake.submit(document, env=self.env)["submission"]["submission_id"]

    def admitted(self, item_id: str, body: str = _READ_REQUEST) -> str:
        sid = self.submit(item_id, body)
        intake_triage.triage(sid, env=self.env)
        return sid

    def write_config(self, extra: dict | None = None) -> None:
        payload = {"schema": product_config.SCHEMA,
                   "provider": {"cli": "opencode", "model": "example/model"}}
        payload.update(extra or {})
        product_config.write_config(payload, product_config.config_path(self.env))

    def authority(self, sid: str, repo: str, **over) -> mock.Mock:
        plan = intake_handoff.handoff_plan(
            sid, repo=repo, env=self.env, dry_run=True)["plan"]
        grant = {
            "plan_digest": intake_handoff._binding_digest(plan),
            "read_only": True,
            "owner": "operator-one",
            "scope": {"repo": repo, "paths": ["a.txt"]},
            "role": "reader",
            "model": "opencode-go/deepseek-v4.1-flash#high",
            "privacy_qualified": True,
            "granted_at": "2026-10-15T00:00:00Z",
        }
        grant.update(over.pop("grant", {}))
        record = {"authenticated": True, "operator": "operator-one",
                  "grant": grant}
        record.update(over)
        return mock.Mock(return_value=record)

    def generic_authority(self, repo: str):
        """An in-process authority that binds whatever plan it is given."""

        def authority(*, submission_id, plan, env=None):
            return {
                "authenticated": True,
                "operator": "operator-one",
                "grant": {
                    "plan_digest": intake_handoff._binding_digest(plan),
                    "read_only": True,
                    "owner": "operator-one",
                    "scope": {"repo": repo, "paths": ["a.txt"]},
                    "role": "reader",
                    "model": "opencode-go/deepseek-v4.1-flash#high",
                    "privacy_qualified": True,
                    "granted_at": "2026-10-15T00:00:00Z",
                },
            }

        return authority

    def bound_manifest(self, sid: str, *, mode: str = "read") -> dict:
        plan = intake_handoff.handoff_plan(
            sid, repo=str(self.repo), env=self.env, dry_run=True)["plan"]
        grant = {"role": "reader", "model": "opencode/model",
                 "repo": str(self.repo), "paths": ["a.txt"]}
        manifest = intake_handoff._manifest(plan, grant)
        if mode == "write":
            task = manifest["tasks"][0]
            task["mode"] = "write"
            task["write_scope"] = ["a.txt"]
            manifest["max_writers"] = 1
        return manifest

    def teams(self) -> list[dict]:
        return team.list_teams(env=self.env)

    def ledger_exists(self) -> bool:
        return Path(self.env["XDG_STATE_HOME"]).joinpath(
            "rsd-work-registry").exists()


class EnqueueTests(QueueCase):
    """A queued intent is derived, source-bound and never authority."""

    def test_eligible_read_is_queued_with_a_bound_source(self) -> None:
        sid = self.admitted("queue-ok")
        result = intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        self.assertEqual(result["status"], "QUEUED")
        self.assertTrue(result["persisted"])
        intent = result["intent"]
        self.assertEqual(intent["status"], "PENDING")
        self.assertEqual(intent["binding"]["submission_id"], sid)
        self.assertEqual(intent["binding"]["source"]["head"],
                         run_git(self.repo, "rev-parse", "HEAD").strip())
        self.assertTrue(intent["binding"]["read_only"])
        self.assertIsNone(intent["approval"])
        self.assertEqual(self.teams(), [])
        self.assertFalse(self.ledger_exists())
        self.assertEqual(
            stat.S_IMODE(Path(intake_queue.queue_dir(self.env)).stat().st_mode),
            0o700,
        )

    def test_replay_is_idempotent_and_never_duplicates(self) -> None:
        sid = self.admitted("queue-replay")
        first = intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        second = intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        self.assertEqual(second["status"], "DUPLICATE")
        self.assertEqual(first["intent_id"], second["intent_id"])
        self.assertEqual(len(intake_queue.list_intents(self.env)), 1)

    def test_non_read_request_is_held_and_persists_nothing(self) -> None:
        sid = self.admitted("queue-mixed", (
            "Read-only audit of the source, then one squash merge of PR 7."))
        result = intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        self.assertEqual(result["status"], "HOLD")
        self.assertFalse(result["persisted"])
        self.assertFalse(intake_queue.list_intents(self.env))
        self.assertFalse(self.teams())

    def test_write_like_request_is_never_queued(self) -> None:
        sid = self.admitted("queue-write", (
            "Apply the correction and push it to the remote branch."))
        result = intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        self.assertEqual(result["status"], "HOLD")
        self.assertFalse(result["persisted"])
        self.assertFalse(self.teams())

    def test_dry_run_creates_no_state(self) -> None:
        sid = self.admitted("queue-dry")
        result = intake_queue.enqueue(
            sid, repo=str(self.repo), env=self.env, dry_run=True)
        self.assertEqual(result["status"], "DRY_RUN")
        self.assertFalse(result["persisted"])
        self.assertFalse(Path(intake_queue.queue_dir(self.env)).exists())

    @unittest.skipUnless(_HAVE_GIT, "git is required")
    def test_repository_without_a_head_is_held(self) -> None:
        sid = self.admitted("queue-nohead")
        empty = self.root / "empty"
        empty.mkdir()
        result = intake_queue.enqueue(sid, repo=str(empty), env=self.env)
        self.assertEqual(result["status"], "HOLD")
        self.assertIn(
            "source_head_binding",
            {hold["code"] for hold in result["holds"]},
        )
        self.assertFalse(intake_queue.list_intents(self.env))

    def test_already_started_read_audit_is_not_queued_twice(self) -> None:
        sid = self.admitted("queue-already")
        manifest = self.bound_manifest(sid)
        team.initialize(manifest, env=self.env)
        result = intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        self.assertEqual(result["status"], "ALREADY_STARTED")
        self.assertFalse(result["persisted"])
        self.assertEqual(len(self.teams()), 1)

    def test_malformed_priority_and_self_dependency_are_refused(self) -> None:
        sid = self.admitted("queue-bad")
        with self.assertRaises(intake_queue.QueueInvalid):
            intake_queue.enqueue(
                sid, repo=str(self.repo), env=self.env, priority=-1)
        with self.assertRaises(intake_queue.QueueInvalid):
            intake_queue.enqueue(
                sid, repo="relative/path", env=self.env)

    def test_queue_is_bounded(self) -> None:
        first = self.admitted("queue-full-a")
        second = self.admitted("queue-full-b")
        intake_queue.enqueue(first, repo=str(self.repo), env=self.env)
        with mock.patch.object(intake_queue, "MAX_ITEMS", 1):
            with self.assertRaises(intake_queue.QueueRefused):
                intake_queue.enqueue(second, repo=str(self.repo), env=self.env)
        self.assertEqual(len(intake_queue.list_intents(self.env)), 1)


class IntegrityTests(QueueCase):
    """Tamper, corruption and a forged approval all fail closed."""

    def test_tampered_record_is_refused(self) -> None:
        sid = self.admitted("queue-tamper")
        intent_id = intake_queue.enqueue(
            sid, repo=str(self.repo), env=self.env)["intent_id"]
        path = Path(intake_queue.intent_path(intent_id, self.env))
        data = json.loads(path.read_text(encoding="utf-8"))
        data["binding"]["team_id"] = "attacker-team"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(intake_queue.QueueStateError):
            intake_queue.read_intent(intent_id, self.env)
        with self.assertRaises(intake_queue.QueueStateError):
            intake_queue.tick(env=self.env)

    def test_corrupt_private_store_fails_closed(self) -> None:
        sid = self.admitted("queue-corrupt")
        intent_id = intake_queue.enqueue(
            sid, repo=str(self.repo), env=self.env)["intent_id"]
        Path(intake_queue.intent_path(intent_id, self.env)).write_text(
            "{not json", encoding="utf-8")
        with self.assertRaises(intake_queue.QueueStateError):
            intake_queue.tick(env=self.env)
        self.assertEqual(self.teams(), [])
        self.assertFalse(self.ledger_exists())

    def test_unexpected_store_file_fails_closed(self) -> None:
        sid = self.admitted("queue-unexpected")
        intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        (Path(intake_queue.queue_dir(self.env)) / "surprise.json").write_text(
            "{}", encoding="utf-8")
        with self.assertRaises(intake_queue.QueueStateError):
            intake_queue.tick(env=self.env)

    def test_digest_mismatch_never_rounds_up(self) -> None:
        sid = self.admitted("queue-digest")
        intent_id = intake_queue.enqueue(
            sid, repo=str(self.repo), env=self.env)["intent_id"]
        path = Path(intake_queue.intent_path(intent_id, self.env))
        data = json.loads(path.read_text(encoding="utf-8"))
        data["status"] = "STARTED"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(intake_queue.QueueStateError):
            intake_queue.read_intent(intent_id, self.env)


class AuthorityGateTests(QueueCase):
    """A queue intent is never its own approval; authority is in-process only."""

    def test_no_authority_holds_with_a_precise_exception(self) -> None:
        sid = self.admitted("gate-none")
        intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        report = intake_queue.tick(env=self.env)
        item = report["items"][0]
        self.assertEqual(item["decision"], "HOLD")
        self.assertEqual(item["code"], "authenticated_operator_authority")
        self.assertEqual(self.teams(), [])
        self.assertFalse(self.ledger_exists())
        self.assertTrue(report["exceptions"])

    def test_in_process_authority_dispatches_exactly_one_read_team(self) -> None:
        sid = self.admitted("gate-ok")
        intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        report = intake_queue.tick(
            env=self.env, operator_authority=self.authority(sid, str(self.repo)))
        item = report["items"][0]
        self.assertEqual(item["decision"], "ADVANCE")
        self.assertEqual(item["status"], "STARTED")
        teams = self.teams()
        self.assertEqual([entry["team_id"] for entry in teams], [item["team_id"]])
        summary = team.status(item["team_id"], env=self.env)
        self.assertEqual(summary["desired"], "running")
        self.assertIsNotNone(item["approval"])

    def test_unauthenticated_and_spoofed_authority_are_refused(self) -> None:
        sid = self.admitted("gate-spoof")
        intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        for forged in (
            mock.Mock(return_value={"authenticated": False}),
            mock.Mock(return_value={"authenticated": True, "operator": "x"}),
            {"authenticated": True},
        ):
            report = intake_queue.tick(
                env=self.env, operator_authority=forged)
            self.assertEqual(report["items"][0]["decision"], "HOLD")
        self.assertEqual(self.teams(), [])

    def test_grant_requiring_write_or_lacking_privacy_is_refused(self) -> None:
        sid = self.admitted("gate-grant")
        intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        for override in ({"read_only": False}, {"privacy_qualified": False}):
            authority = self.authority(sid, str(self.repo),
                                       grant=dict(override))
            report = intake_queue.tick(
                env=self.env, operator_authority=authority)
            self.assertEqual(report["items"][0]["decision"], "HOLD")
            self.assertEqual(report["items"][0]["code"], "dispatch_refused")
        self.assertEqual(self.teams(), [])

    def test_model_allowlist_refusal_holds_without_a_team(self) -> None:
        self.write_config({"team_model_allowlist": ["opencode-go/other-model"]})
        sid = self.admitted("gate-model")
        intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        report = intake_queue.tick(
            env=self.env, operator_authority=self.authority(sid, str(self.repo)))
        self.assertEqual(report["items"][0]["decision"], "HOLD")
        self.assertFalse(self.teams())

    def test_wrong_scope_repository_is_refused(self) -> None:
        sid = self.admitted("gate-scope")
        intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        other = make_repo(self.root, "other")
        report = intake_queue.tick(
            env=self.env, operator_authority=self.authority(sid, str(other),
                                                            grant={"scope":
                                                                   {"repo": str(other),
                                                                    "paths": []}}))
        self.assertEqual(report["items"][0]["decision"], "HOLD")
        self.assertFalse(self.teams())


class SafetyGateTests(QueueCase):
    """Every stale/conflicted/dependent item blocks only itself."""

    @unittest.skipUnless(_HAVE_GIT, "git is required")
    def test_stale_source_head_holds_and_starts_nothing(self) -> None:
        sid = self.admitted("safe-drift")
        intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        (self.repo / "a.txt").write_text("two\n", encoding="utf-8")
        commit_all(self.repo, "move")
        report = intake_queue.tick(
            env=self.env, operator_authority=self.authority(sid, str(self.repo)))
        self.assertEqual(report["items"][0]["decision"], "HOLD")
        self.assertEqual(report["items"][0]["code"], "source_head_drift")
        self.assertFalse(self.teams())

    def test_changed_route_binding_holds(self) -> None:
        self.write_config({"provider": {"cli": "opencode", "id": "provider-x",
                                        "model": "example/model"}})
        sid = self.admitted("safe-binding")
        intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        # A changed provider selection changes the qualification the intent bound.
        self.write_config({"provider": {"cli": "opencode", "id": "provider-y",
                                        "model": "example/model"}})
        report = intake_queue.tick(
            env=self.env,
            operator_authority=self.generic_authority(str(self.repo)))
        self.assertEqual(report["items"][0]["decision"], "HOLD")
        self.assertEqual(report["items"][0]["code"], "intake_binding_changed")
        self.assertFalse(self.teams())

    def test_write_manifest_with_the_bound_id_is_never_started(self) -> None:
        sid = self.admitted("safe-write")
        intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        manifest = self.bound_manifest(sid, mode="write")
        team.initialize(manifest, env=self.env)
        report = intake_queue.tick(env=self.env)
        self.assertEqual(report["items"][0]["decision"], "HOLD")
        self.assertEqual(report["items"][0]["code"], "not_read_only")
        self.assertEqual(team.status(manifest["team_id"], env=self.env)["desired"],
                         "paused")

    def test_running_write_manifest_is_reported_not_advanced(self) -> None:
        sid = self.admitted("safe-write-running")
        intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        manifest = self.bound_manifest(sid, mode="write")
        team.initialize(manifest, env=self.env)
        team.set_desired(manifest["team_id"], "running", env=self.env)
        report = intake_queue.tick(env=self.env)
        self.assertEqual(report["items"][0]["decision"], "HOLD")
        self.assertEqual(report["items"][0]["code"], "not_read_only")

    def test_missing_dependency_holds(self) -> None:
        sid = self.admitted("safe-missing")
        intake_queue.enqueue(sid, repo=str(self.repo), env=self.env,
                             depends_on=["a" * 64])
        report = intake_queue.tick(
            env=self.env, operator_authority=self.authority(sid, str(self.repo)))
        self.assertEqual(report["items"][0]["code"], "dependency_missing")
        self.assertFalse(self.teams())

    def test_cycle_holds_every_member(self) -> None:
        first = self.admitted("cycle-a")
        second = self.admitted("cycle-b")
        second_id = intake_queue.enqueue(
            second, repo=str(self.repo), env=self.env, dry_run=True)["intent_id"]
        first_id = intake_queue.enqueue(
            first, repo=str(self.repo), env=self.env,
            depends_on=[second_id])["intent_id"]
        intake_queue.enqueue(second, repo=str(self.repo), env=self.env,
                             depends_on=[first_id])
        report = intake_queue.tick(
            env=self.env, operator_authority=self.authority(first, str(self.repo)))
        codes = {item["code"] for item in report["items"]}
        self.assertEqual(codes, {"dependency_cycle"})
        self.assertFalse(self.teams())

    def test_unmet_dependency_isolates_but_independent_item_advances(self) -> None:
        dep = self.admitted("iso-dep")
        dep_id = intake_queue.enqueue(
            dep, repo=str(self.repo), env=self.env, priority=200)["intent_id"]
        blocked = self.admitted("iso-blocked")
        intake_queue.enqueue(blocked, repo=str(self.repo), env=self.env,
                             priority=1, depends_on=[dep_id])
        ready = self.admitted("iso-ready")
        intake_queue.enqueue(ready, repo=str(self.repo), env=self.env,
                             priority=50)
        report = intake_queue.tick(
            env=self.env, operator_authority=self.generic_authority(str(self.repo)),
            budget=1)
        blocked_item = next(item for item in report["items"]
                            if item["submission_id"].startswith(blocked[:12]))
        self.assertEqual(blocked_item["code"], "dependency_unmet")
        advanced = [item for item in report["items"]
                    if item["decision"] == "ADVANCE"]
        self.assertEqual(len(advanced), 1)
        self.assertEqual(advanced[0]["submission_id"], ready)
        self.assertEqual(len(self.teams()), 1)

    def test_registry_conflict_holds(self) -> None:
        sid = self.admitted("safe-registry")
        intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        conflict = {"verdict": "SUPPORTED", "conflict": True,
                    "against_run": "cao-team-other", "reason": "overlap"}
        with mock.patch.object(intake_queue, "_registry_conflict",
                               return_value=conflict):
            report = intake_queue.tick(
                env=self.env,
                operator_authority=self.authority(sid, str(self.repo)))
        self.assertEqual(report["items"][0]["code"], "registry_conflict")
        self.assertFalse(self.teams())

    def test_unreadable_registry_holds(self) -> None:
        sid = self.admitted("safe-registry-unknown")
        intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        unknown = {"verdict": "UNKNOWN", "conflict": False, "reason": "unreadable"}
        with mock.patch.object(intake_queue, "_registry_conflict",
                               return_value=unknown):
            report = intake_queue.tick(
                env=self.env,
                operator_authority=self.authority(sid, str(self.repo)))
        self.assertEqual(report["items"][0]["code"], "registry_unavailable")
        self.assertFalse(self.teams())

    def test_same_checkout_active_claim_holds_via_the_real_registry_read(self) -> None:
        sid = self.admitted("safe-registry-real")
        intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        ledger = self.root / "registry.sqlite3"
        ledger.write_bytes(b"")
        claim = {"run_id": "cao-team-other", "work_key": "other",
                 "repo": str(self.repo), "paths": ["a.txt"], "mode": "WRITE",
                 "objective": "unrelated"}
        with mock.patch.object(intake_queue.registry, "connect_readonly",
                               return_value=mock.Mock()), \
             mock.patch.object(intake_queue.registry, "list_active_claims",
                               return_value=[claim]):
            report = intake_queue.tick(
                env=self.env, registry_db=str(ledger),
                operator_authority=self.generic_authority(str(self.repo)))
        self.assertEqual(report["items"][0]["code"], "registry_conflict")
        self.assertFalse(self.teams())

    def test_budget_defers_an_eligible_item(self) -> None:
        first = self.admitted("budget-a")
        second = self.admitted("budget-b")
        intake_queue.enqueue(first, repo=str(self.repo), env=self.env, priority=1)
        intake_queue.enqueue(second, repo=str(self.repo), env=self.env, priority=2)
        report = intake_queue.tick(
            env=self.env,
            operator_authority=self.generic_authority(str(self.repo)),
            budget=1)
        self.assertEqual(report["advanced"], 1)
        deferred = [item for item in report["items"]
                    if item["decision"] == "NONE" and item["code"] == "budget"]
        self.assertEqual(len(deferred), 1)
        self.assertEqual(len(self.teams()), 1)

    def test_concurrent_pass_reports_locked_and_mutates_nothing(self) -> None:
        sid = self.admitted("concurrent")
        intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        directory = intake_queue.queue_dir(self.env)
        fd = os.open(os.path.join(directory, ".lock"),
                     os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            report = intake_queue.tick(
                env=self.env,
                operator_authority=self.authority(sid, str(self.repo)))
        finally:
            os.close(fd)
        self.assertEqual(report["status"], "LOCKED")
        self.assertEqual(report["items"], [])
        self.assertFalse(self.teams())


class AutoAdvanceTests(QueueCase):
    """An already-approved immutable READ team re-verifies before it starts."""

    def _started_team(self, item_id: str) -> tuple[str, dict]:
        self.write_config({"team_approval": {"mode": "chat"}})
        sid = self.admitted(item_id)
        intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        manifest = self.bound_manifest(sid)
        config = product_config.load_config(product_config.config_path(self.env))
        team.initialize(manifest, env=self.env, config=config, approved=True)
        return sid, manifest

    def test_paused_approved_team_auto_advances(self) -> None:
        _sid, manifest = self._started_team("auto")
        report = intake_queue.tick(env=self.env)
        item = report["items"][0]
        self.assertEqual(item["decision"], "ADVANCE")
        self.assertIsNotNone(item["approval"])
        self.assertEqual(team.status(manifest["team_id"], env=self.env)["desired"],
                         "running")
        self.assertFalse(self.ledger_exists())

    def test_forged_approval_is_refused(self) -> None:
        _sid, manifest = self._started_team("auto-forged")
        record = team._read(manifest["team_id"], self.env)
        record["admission"] = dict(record["admission"])
        record["admission"]["spec_sha256"] = "0" * 64
        record["desired"] = "paused"
        team._save(record, self.env)
        report = intake_queue.tick(env=self.env)
        self.assertEqual(report["items"][0]["decision"], "HOLD")
        self.assertEqual(report["items"][0]["code"], "approval_unverified")
        self.assertEqual(team.status(manifest["team_id"], env=self.env)["desired"],
                         "paused")

    def test_repeated_ticks_never_duplicate_the_team(self) -> None:
        sid, manifest = self._started_team("auto-repeat")
        first = intake_queue.tick(
            env=self.env, operator_authority=self.authority(sid, str(self.repo)))
        second = intake_queue.tick(
            env=self.env, operator_authority=self.authority(sid, str(self.repo)))
        self.assertEqual(first["items"][0]["decision"], "ADVANCE")
        self.assertEqual(second["items"][0]["decision"], "NONE")
        self.assertEqual(len(self.teams()), 1)
        self.assertTrue(team.status(manifest["team_id"], env=self.env)["desired"]
                        == "running")


class DaemonOptInTests(QueueCase):
    """The queue is reconciled by the existing daemon and is opt-in."""

    def _daemon(self, config):
        return product_daemon.Daemon(
            driver=None, config=config, env=self.env,
            registry_db=str(self.root / "registry.sqlite3"),
            sleep=lambda _seconds: None, spawn=lambda: 0,
            alive=lambda _pid: False, terminate=lambda _pid: None)

    def _config(self, **daemon):
        return product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "model": "example/model"},
            "daemon": daemon,
        })

    def _running_daemon_record(self) -> None:
        product_state.write_daemon({
            **product_state.default_daemon_record(),
            "desired": "running", "observed": "running",
        }, self.env)

    def test_default_is_disabled(self) -> None:
        self.assertFalse(product_config.daemon_defaults(
            self._config()).intake_queue)
        sid = self.admitted("daemon-disabled")
        intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        self._running_daemon_record()
        with mock.patch("cao.intake_queue.tick") as tick:
            report = self._daemon(self._config()).tick()
        tick.assert_not_called()
        self.assertFalse(any(item.get("action", "").startswith("intake_queue")
                             for item in report["effects"]))
        self.assertFalse(self.teams())

    def test_enabled_daemon_reconciles_and_passes_no_authority(self) -> None:
        sid = self.admitted("daemon-enabled")
        intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        self._running_daemon_record()
        daemon = self._daemon(self._config(intake_queue=True))
        with mock.patch("cao.intake_queue.tick",
                        wraps=intake_queue.tick) as tick:
            report = daemon.tick()
        tick.assert_called_once()
        self.assertNotIn("operator_authority", tick.call_args.kwargs)
        self.assertTrue(any(item.get("action") == "intake_queue"
                            and item.get("decision") == "HOLD"
                            for item in report["effects"]))
        self.assertFalse(self.teams())
        self.assertFalse(self.ledger_exists())

    def test_daemon_flag_must_be_boolean(self) -> None:
        with self.assertRaises(product_config.ProductConfigError):
            product_config.daemon_defaults(
                self._config(intake_queue="yes"))


class CliTests(QueueCase):
    def run_cli(self, argv: list[str], env: dict | None = None) -> tuple[int, dict]:
        from cao import intake_cli

        stream = io.StringIO()
        with mock.patch.dict(os.environ, env or self.env):
            with contextlib.redirect_stdout(stream):
                code = intake_cli.main(argv)
        return code, json.loads(stream.getvalue())

    def test_enqueue_list_and_show(self) -> None:
        sid = self.admitted("cli-queue")
        code, payload = self.run_cli(
            ["queue", "enqueue", "--id", sid, "--repo", str(self.repo)])
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "QUEUED")
        intent_id = payload["intent_id"]
        code, payload = self.run_cli(["queue", "list"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 1)
        code, payload = self.run_cli(["queue", "show", intent_id])
        self.assertEqual(code, 0)
        self.assertEqual(payload["intent"]["intent_id"], intent_id)
        # The CLI never dispatches: no team is created by an enqueue.
        self.assertFalse(self.teams())

    def test_cli_holds_a_non_read_request(self) -> None:
        sid = self.admitted("cli-mixed", (
            "Read-only audit then one squash merge of PR 7."))
        code, payload = self.run_cli(
            ["queue", "enqueue", "--id", sid, "--repo", str(self.repo)])
        self.assertEqual(code, 3)
        self.assertEqual(payload["status"], "HOLD")

    def test_cli_environment_is_never_authority(self) -> None:
        sid = self.admitted("cli-env")
        env = dict(self.env, CAO_OPERATOR="operator-one",
                   CAO_OPERATOR_AUTHORITY="granted")
        code, payload = self.run_cli(
            ["queue", "enqueue", "--id", sid, "--repo", str(self.repo)], env)
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "QUEUED")
        self.assertFalse(self.teams())

    def test_show_missing_intent_is_unavailable(self) -> None:
        code, payload = self.run_cli(["queue", "show", "a" * 64])
        self.assertEqual(code, 3)


if __name__ == "__main__":
    unittest.main()
