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

    def complete_team(self, manifest: dict, *, head: str) -> None:
        """Mark a bound team's single task COMPLETE at an accepted revision."""

        record = team._read(manifest["team_id"], self.env)
        task = manifest["tasks"][0]
        record["slots"][task["id"]].update(
            status="complete", head=head, prompt="synthetic",
            execution_id=team._execution_id(
                manifest["team_id"], task["id"], "work", 0),
        )
        team._save(record, self.env)

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

    def test_replay_with_changed_input_is_refused_and_preserved(self) -> None:
        # The intent id is a digest of the immutable binding, so a replay that
        # keeps the binding but changes priority or dependencies is never
        # silently reported as an identical duplicate; the stored record and its
        # receipt are preserved untouched.
        sid = self.admitted("queue-replay-diff")
        first = intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        self.assertEqual(first["status"], "QUEUED")
        with self.assertRaises(intake_queue.QueueRefused):
            intake_queue.enqueue(sid, repo=str(self.repo), env=self.env,
                                 priority=first["intent"]["priority"] + 1)
        with self.assertRaises(intake_queue.QueueRefused):
            intake_queue.enqueue(sid, repo=str(self.repo), env=self.env,
                                 depends_on=["a" * 64])
        stored = intake_queue.read_intent(first["intent_id"], self.env)
        self.assertEqual(stored["priority"], intake_queue.DEFAULT_PRIORITY)
        self.assertEqual(stored["depends_on"], [])
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

    def test_conflicting_claim_past_the_former_cut_still_holds(self) -> None:
        # A conflicting active claim beyond the former fixed cut must still be
        # seen; a truncated scan would fail open and advance the item.
        sid = self.admitted("safe-registry-past-cut")
        intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        ledger = self.root / "registry.sqlite3"
        ledger.write_bytes(b"")
        unrelated = [
            {"run_id": f"cao-team-unrelated-{n}", "work_key": f"other-{n}",
             "repo": str(self.root / f"elsewhere-{n}"), "paths": [],
             "mode": "READ", "objective": "unrelated"}
            for n in range(70)
        ]
        conflict = {"run_id": "cao-team-other", "work_key": "other",
                    "repo": str(self.repo), "paths": ["a.txt"], "mode": "WRITE",
                    "objective": "unrelated"}
        claims = unrelated[:64] + [conflict]
        with mock.patch.object(intake_queue.registry, "connect_readonly",
                               return_value=mock.Mock()), \
             mock.patch.object(intake_queue.registry, "list_active_claims",
                               return_value=claims):
            report = intake_queue.tick(
                env=self.env, registry_db=str(ledger),
                operator_authority=self.generic_authority(str(self.repo)))
        self.assertEqual(report["items"][0]["code"], "registry_conflict")
        self.assertFalse(self.teams())

    def test_concurrent_enqueue_is_serialized_by_the_pass_lock(self) -> None:
        first = self.admitted("enqueue-lock-a")
        second = self.admitted("enqueue-lock-b")
        with mock.patch.object(intake_queue, "MAX_ITEMS", 1):
            intake_queue.enqueue(first, repo=str(self.repo), env=self.env)
            directory = intake_queue.queue_dir(self.env)
            fd = os.open(os.path.join(directory, ".lock"),
                         os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(intake_queue.QueueBusy):
                    intake_queue.enqueue(second, repo=str(self.repo), env=self.env)
            finally:
                os.close(fd)
        self.assertEqual(len(intake_queue.list_intents(self.env)), 1)

    def test_paused_read_team_for_an_unrelated_repo_is_held(self) -> None:
        # A PAUSED READ team whose manifest lists another checkout must never be
        # started as this intent's audit: the source binding is exact.
        self.write_config({"team_approval": {"mode": "chat"}})
        sid = self.admitted("bind-repo-paused")
        intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        other = make_repo(self.root, "unrelated")
        manifest = self.bound_manifest(sid)
        manifest["tasks"][0]["repo"] = str(other)
        config = product_config.load_config(product_config.config_path(self.env))
        team.initialize(manifest, env=self.env, config=config, approved=True)
        report = intake_queue.tick(env=self.env)
        self.assertEqual(report["items"][0]["decision"], "HOLD")
        self.assertEqual(report["items"][0]["code"], "source_mismatch")
        self.assertEqual(team.status(manifest["team_id"], env=self.env)["desired"],
                         "paused")

    def test_running_read_team_for_an_unrelated_repo_is_held(self) -> None:
        self.write_config({"team_approval": {"mode": "chat"}})
        sid = self.admitted("bind-repo-running")
        intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        other = make_repo(self.root, "cross-source")
        manifest = self.bound_manifest(sid)
        manifest["tasks"][0]["repo"] = str(other)
        config = product_config.load_config(product_config.config_path(self.env))
        team.initialize(manifest, env=self.env, config=config, approved=True)
        team.set_desired(manifest["team_id"], "running", env=self.env)
        report = intake_queue.tick(env=self.env)
        self.assertEqual(report["items"][0]["decision"], "HOLD")
        self.assertEqual(report["items"][0]["code"], "source_mismatch")

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

    def test_zero_budget_starts_no_team_and_reports_no_advance(self) -> None:
        self.write_config({"team_approval": {"mode": "chat"}})
        sid = self.admitted("budget-zero-team")
        intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        manifest = self.bound_manifest(sid)
        config = product_config.load_config(product_config.config_path(self.env))
        team.initialize(manifest, env=self.env, config=config, approved=True)
        report = intake_queue.tick(env=self.env, budget=0)
        self.assertEqual(report["advanced"], 0)
        self.assertEqual(report["items"][0]["decision"], "NONE")
        self.assertEqual(report["items"][0]["code"], "budget")
        self.assertEqual(team.status(manifest["team_id"], env=self.env)["desired"],
                         "paused")

    def test_zero_budget_dispatches_no_team(self) -> None:
        sid = self.admitted("budget-zero-dispatch")
        intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        report = intake_queue.tick(
            env=self.env, budget=0,
            operator_authority=self.generic_authority(str(self.repo)))
        self.assertEqual(report["advanced"], 0)
        self.assertEqual(report["items"][0]["decision"], "NONE")
        self.assertEqual(report["items"][0]["code"], "budget")
        self.assertFalse(self.teams())

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

    def test_bound_but_unapproved_team_is_held(self) -> None:
        # Legacy approval mode: a bound READ manifest is initialized with no
        # persisted admission, so the queue holds it rather than start it.
        sid = self.admitted("auto-unapproved")
        intake_queue.enqueue(sid, repo=str(self.repo), env=self.env)
        manifest = self.bound_manifest(sid)
        team.initialize(manifest, env=self.env)
        record = team._read(manifest["team_id"], self.env)
        self.assertIsNone(record.get("admission"))
        report = intake_queue.tick(env=self.env)
        self.assertEqual(report["items"][0]["decision"], "HOLD")
        self.assertEqual(report["items"][0]["code"], "team_unapproved")
        self.assertEqual(team.status(manifest["team_id"], env=self.env)["desired"],
                         "paused")

    def test_unapproved_complete_team_never_grants_dependency_acceptance(self) -> None:
        # A team the operator never approved, even one labeled COMPLETE, must
        # not mark its intent terminal and so must not release a dependent.
        dep = self.admitted("terminal-unapproved")
        dep_id = intake_queue.enqueue(
            dep, repo=str(self.repo), env=self.env, priority=200)["intent_id"]
        child = self.admitted("terminal-child")
        intake_queue.enqueue(child, repo=str(self.repo), env=self.env,
                             priority=1, depends_on=[dep_id])
        manifest = self.bound_manifest(dep)
        team.initialize(manifest, env=self.env)  # legacy: no persisted admission
        head = run_git(self.repo, "rev-parse", "HEAD").strip()
        self.complete_team(manifest, head=head)
        self.assertEqual(team.status(manifest["team_id"], env=self.env)["status"],
                         "COMPLETE")
        report = intake_queue.tick(env=self.env)
        by_sid = {item["submission_id"]: item for item in report["items"]}
        self.assertEqual(by_sid[dep]["decision"], "HOLD")
        self.assertEqual(by_sid[dep]["code"], "team_unapproved")
        self.assertEqual(by_sid[child]["code"], "dependency_unmet")

    def test_completed_team_on_a_drifted_revision_is_held(self) -> None:
        # A COMPLETE label is terminal evidence only for the intent's exact
        # bound HEAD; a completed revision that is not that HEAD is not accepted
        # and does not release a dependent.
        sid, manifest = self._started_team("auto-drift")
        self.complete_team(manifest, head="0" * 40)
        report = intake_queue.tick(env=self.env)
        self.assertEqual(report["items"][0]["decision"], "HOLD")
        self.assertEqual(report["items"][0]["code"], "source_head_drift")


class DependencyEvidenceTests(QueueCase):
    """A COMPLETE dependency is re-derived from its team, never from its status.

    The stored queue status and its ordinary content digest are caller-writable,
    so a forged terminal status with no authoritative CAO team -- or a foreign
    or stale team -- must hold the dependent only and never release it or create
    a child team or Registry claim.
    """

    def _approved_dependency(self, item_id: str) -> tuple[str, str, dict]:
        """Admit a dependency whose approved team is marked COMPLETE at HEAD."""

        self.write_config({"team_approval": {"mode": "chat"}})
        sid = self.admitted(item_id)
        dep_id = intake_queue.enqueue(
            sid, repo=str(self.repo), env=self.env, priority=200)["intent_id"]
        manifest = self.bound_manifest(sid)
        config = product_config.load_config(
            product_config.config_path(self.env))
        team.initialize(manifest, env=self.env, config=config, approved=True)
        head = run_git(self.repo, "rev-parse", "HEAD").strip()
        self.complete_team(manifest, head=head)
        return sid, dep_id, manifest

    def _enqueue_child(self, item_id: str, dep_id: str) -> tuple[str, str]:
        child = self.admitted(item_id)
        child_id = intake_queue.enqueue(
            child, repo=str(self.repo), env=self.env,
            priority=1, depends_on=[dep_id])["intent_id"]
        return child, child_id

    def _forge_complete_status(self, intent_id: str) -> None:
        """Set a stored terminal status with its ordinary content digest."""

        record = intake_queue.read_intent(intent_id, self.env)
        record["status"] = "COMPLETE"
        record["digest"] = intake_queue._intent_digest(record)
        intake_queue._write_intent(record, self.env)

    def test_forged_complete_dependency_without_a_team_never_releases_a_child(self):
        # The synthetic bypass: store COMPLETE and recompute the digest with no
        # CAO team at all, then tick the child with an authenticated READ
        # authority. The child must be held, and no child team or claim created.
        dep = self.admitted("dep-forged")
        dep_id = intake_queue.enqueue(
            dep, repo=str(self.repo), env=self.env, priority=200)["intent_id"]
        child, _child_id = self._enqueue_child("dep-forged-child", dep_id)
        self._forge_complete_status(dep_id)
        report = intake_queue.tick(
            env=self.env,
            operator_authority=self.generic_authority(str(self.repo)))
        by_sid = {item["submission_id"]: item for item in report["items"]}
        self.assertEqual(by_sid[child]["decision"], "HOLD")
        self.assertEqual(by_sid[child]["code"], "dependency_unverified")
        self.assertIsNone(team._read(by_sid[child]["team_id"], self.env))
        self.assertEqual(self.teams(), [])
        self.assertFalse(self.ledger_exists())

    def test_foreign_complete_dependency_stays_held(self):
        other = make_repo(self.root, "foreign")
        self.write_config({"team_approval": {"mode": "chat"}})
        dep = self.admitted("dep-foreign")
        dep_id = intake_queue.enqueue(
            dep, repo=str(self.repo), env=self.env, priority=200)["intent_id"]
        manifest = self.bound_manifest(dep)
        manifest["tasks"][0]["repo"] = str(other)
        config = product_config.load_config(
            product_config.config_path(self.env))
        team.initialize(manifest, env=self.env, config=config, approved=True)
        self.complete_team(manifest,
                           head=run_git(other, "rev-parse", "HEAD").strip())
        child, _child_id = self._enqueue_child("dep-foreign-child", dep_id)
        self._forge_complete_status(dep_id)
        report = intake_queue.tick(
            env=self.env,
            operator_authority=self.generic_authority(str(self.repo)))
        by_sid = {item["submission_id"]: item for item in report["items"]}
        self.assertEqual(by_sid[child]["decision"], "HOLD")
        self.assertEqual(by_sid[child]["code"], "dependency_unverified")
        self.assertIsNone(team._read(by_sid[child]["team_id"], self.env))
        self.assertFalse(self.ledger_exists())

    def test_bound_but_not_complete_dependency_stays_held(self):
        # An approved, bound READ team that is only PAUSED is not terminal
        # evidence, even when the dependency's stored status claims COMPLETE.
        self.write_config({"team_approval": {"mode": "chat"}})
        dep = self.admitted("dep-paused")
        dep_id = intake_queue.enqueue(
            dep, repo=str(self.repo), env=self.env, priority=200)["intent_id"]
        manifest = self.bound_manifest(dep)
        config = product_config.load_config(
            product_config.config_path(self.env))
        team.initialize(manifest, env=self.env, config=config, approved=True)
        child, _child_id = self._enqueue_child("dep-paused-child", dep_id)
        self._forge_complete_status(dep_id)
        report = intake_queue.tick(
            env=self.env,
            operator_authority=self.generic_authority(str(self.repo)))
        by_sid = {item["submission_id"]: item for item in report["items"]}
        self.assertEqual(by_sid[child]["decision"], "HOLD")
        self.assertEqual(by_sid[child]["code"], "dependency_unverified")
        self.assertIsNone(team._read(by_sid[child]["team_id"], self.env))
        self.assertFalse(self.ledger_exists())

    def test_complete_dependency_after_a_source_move_stays_held(self):
        dep, dep_id, _manifest = self._approved_dependency("dep-stale")
        child, _child_id = self._enqueue_child("dep-stale-child", dep_id)
        # First tick proves the dependency complete at its bound HEAD.
        first = intake_queue.tick(env=self.env)
        self.assertEqual(
            {item["submission_id"]: item["status"]
             for item in first["items"]}[dep], "COMPLETE")
        # The audited source then moves; the stored COMPLETE is now stale and
        # the dependency's own team evidence no longer matches its bound HEAD.
        (self.repo / "a.txt").write_text("two\n", encoding="utf-8")
        commit_all(self.repo, "move")
        report = intake_queue.tick(
            env=self.env,
            operator_authority=self.generic_authority(str(self.repo)))
        by_sid = {item["submission_id"]: item for item in report["items"]}
        self.assertEqual(by_sid[child]["decision"], "HOLD")
        self.assertEqual(by_sid[child]["code"], "dependency_unverified")
        self.assertIsNone(team._read(by_sid[child]["team_id"], self.env))
        self.assertFalse(self.ledger_exists())

    def test_approved_complete_dependency_unlocks_a_child_on_the_next_tick(self):
        dep, dep_id, _manifest = self._approved_dependency("dep-ok")
        child, _child_id = self._enqueue_child("dep-ok-child", dep_id)
        first = intake_queue.tick(env=self.env)
        by_sid = {item["submission_id"]: item for item in first["items"]}
        self.assertEqual(by_sid[child]["code"], "dependency_unmet")
        self.assertEqual(by_sid[dep]["status"], "COMPLETE")
        # Next tick: the dependency's team checkpoint proves completion and the
        # eligible child advances through the one bounded path.
        second = intake_queue.tick(
            env=self.env,
            operator_authority=self.generic_authority(str(self.repo)))
        by_sid = {item["submission_id"]: item for item in second["items"]}
        self.assertEqual(by_sid[child]["decision"], "ADVANCE")
        self.assertEqual(by_sid[child]["status"], "STARTED")
        self.assertIsNotNone(team._read(by_sid[child]["team_id"], self.env))
        self.assertFalse(self.ledger_exists())

    def test_independent_work_advances_while_a_forged_dependency_holds(self):
        dep = self.admitted("dep-iso")
        dep_id = intake_queue.enqueue(
            dep, repo=str(self.repo), env=self.env, priority=200)["intent_id"]
        child, _child_id = self._enqueue_child("dep-iso-child", dep_id)
        ready = self.admitted("dep-iso-ready")
        intake_queue.enqueue(ready, repo=str(self.repo), env=self.env,
                             priority=50)
        self._forge_complete_status(dep_id)
        report = intake_queue.tick(
            env=self.env,
            operator_authority=self.generic_authority(str(self.repo)),
            budget=1)
        by_sid = {item["submission_id"]: item for item in report["items"]}
        self.assertEqual(by_sid[child]["decision"], "HOLD")
        self.assertEqual(by_sid[child]["code"], "dependency_unverified")
        advanced = [item for item in report["items"]
                    if item["decision"] == "ADVANCE"]
        self.assertEqual([item["submission_id"] for item in advanced], [ready])
        self.assertEqual(len(self.teams()), 1)
        self.assertIsNone(team._read(by_sid[child]["team_id"], self.env))
        self.assertFalse(self.ledger_exists())


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


class CitationGateTests(QueueCase):
    """B2d: the opt-in pseudonymous citation proves an alias manifest without
    weakening the default foreign-team / READ-only / source gates."""

    CITATION = "synthetic-" + "d" * 40

    def plan_binding(self, sid: str) -> dict:
        return intake_handoff.handoff_plan(
            sid, repo=str(self.repo), env=self.env, dry_run=True
        )["plan"]["binding"]

    def alias_manifest(self, sid: str, *, mode: str = "read",
                       repo: str | None = None) -> dict:
        plan = intake_handoff.handoff_plan(
            sid, repo=str(self.repo), env=self.env, dry_run=True)["plan"]
        grant = {"role": "reader", "model": "opencode/model",
                 "repo": repo or str(self.repo), "paths": ["a.txt"]}
        manifest = intake_handoff._manifest(plan, grant, citation=self.CITATION)
        if mode == "write":
            task = manifest["tasks"][0]
            task["mode"] = "write"
            task["write_scope"] = ["a.txt"]
            manifest["max_writers"] = 1
        return manifest

    def test_default_gate_refuses_an_alias_only_manifest(self) -> None:
        sid = self.admitted("alias-queue-default")
        spec = self.alias_manifest(sid)
        self.assertFalse(intake_queue._is_read_manifest_for(spec, sid))
        self.assertTrue(intake_queue._is_read_manifest_for(
            spec, sid, citation=self.CITATION))
        self.assertIn(self.CITATION, spec["tasks"][0]["prompt"])
        self.assertNotIn(sid, spec["tasks"][0]["prompt"])

    def test_bound_team_hold_accepts_the_citation_only_when_supplied(self) -> None:
        sid = self.admitted("alias-queue-bound")
        self.write_config({"team_approval": {"mode": "chat"}})
        manifest = self.alias_manifest(sid)
        config = product_config.load_config(product_config.config_path(self.env))
        team.initialize(manifest, env=self.env, config=config, approved=True)
        record = team._read(manifest["team_id"], self.env)
        binding = self.plan_binding(sid)
        held = intake_queue._bound_read_team_hold(record, binding, sid)
        self.assertIsNotNone(held)
        self.assertEqual(held["code"], "not_read_only")
        # The opt-in citation clears only the citation gate; the admission and
        # source gates still apply and both pass here.
        self.assertIsNone(intake_queue._bound_read_team_hold(
            record, binding, sid, citation=self.CITATION))

    def test_citation_never_weakens_the_read_only_or_source_gates(self) -> None:
        sid = self.admitted("alias-queue-write")
        write = self.alias_manifest(sid, mode="write")
        self.assertFalse(intake_queue._is_read_manifest_for(
            write, sid, citation=self.CITATION))
        cross = self.alias_manifest(sid, repo=str(self.root / "elsewhere"))
        binding = self.plan_binding(sid)
        held = intake_queue._team_source_hold(
            cross, binding, sid, citation=self.CITATION)
        self.assertIsNotNone(held)
        self.assertEqual(held["code"], "source_mismatch")


if __name__ == "__main__":
    unittest.main()
