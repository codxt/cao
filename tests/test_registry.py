"""Focused synthetic tests for the portable Work Registry (``cao.registry``).

These tests build real, throwaway Git repositories so repository identity,
occupancy and drift are exercised against actual Git state, never a stub.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cao import git
from cao import registry
from cao.registry import (
    AgentObservation,
    ClaimSpec,
    RegistryError,
    RuntimeObservation,
)


def run_git(cwd: os.PathLike | str, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.stdout.decode("utf-8", "replace")


def make_repo(root: Path, name: str = "repo") -> Path:
    path = root / name
    path.mkdir(parents=True)
    run_git(path, "init", "-b", "main")
    run_git(path, "config", "user.email", "test@example.invalid")
    run_git(path, "config", "user.name", "Test")
    run_git(path, "config", "commit.gpgsign", "false")
    (path / "src").mkdir()
    (path / "src" / "app.py").write_text("print('one')\n", encoding="utf-8")
    (path / "outside.txt").write_text("outside\n", encoding="utf-8")
    run_git(path, "add", "-A")
    run_git(path, "commit", "-m", "init")
    return path


EMPTY = RuntimeObservation.empty()


class RegistryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = make_repo(self.root)
        self.db = str(self.root / "state" / "registry.sqlite3")
        self.conn = registry.connect(self.db)
        self.addCleanup(self.conn.close)

    def propose(self, work_key: str, paths, **kwargs):
        return registry.propose(self.conn, "run", work_key, str(self.repo),
                                paths=paths, **kwargs)

    def claim(self, work_key: str, owner: str, mode: str = "WRITE",
              observation=EMPTY, **kwargs):
        return registry.claim(
            self.conn,
            ClaimSpec(run_id="run", work_key=work_key, owner=owner, mode=mode,
                      **kwargs),
            observation)

    def evidence(self, observation=EMPTY) -> dict:
        # Bound to this repo's worktree, which the write gate now requires.
        return registry.gather_evidence(str(self.repo), observation,
                                        worktree=str(self.repo))


class ConflictAndScopeTests(RegistryTestCase):
    def test_writer_conflict_refused(self) -> None:
        # Two different checkouts on two different branches: the only overlap
        # left to refuse is the declared path scope.
        linked = self.root / "linked"
        run_git(self.repo, "worktree", "add", "-b", "feature", str(linked))
        self.propose("a", ["src"])
        self.propose("b", ["src/app.py"])
        self.assertEqual(
            self.claim("a", "alice", worktree=str(self.repo),
                       branch="main")["status"], registry.CLAIMED)
        second = self.claim("b", "bob", worktree=str(linked),
                            branch="feature")
        self.assertEqual(second["status"], registry.CONFLICT)
        self.assertEqual(second["conflicts"][0]["kind"], "path")

    def test_disjoint_scopes_are_allowed(self) -> None:
        # One accountable owner holding two writer claims side by side in one
        # checkout on one branch: disjoint paths are compatible.
        self.propose("a", ["src"])
        self.propose("b", ["lib"])
        self.assertEqual(
            self.claim("a", "alice", worktree=str(self.repo),
                       branch="main")["status"], registry.CLAIMED)
        self.assertEqual(
            self.claim("b", "alice", worktree=str(self.repo),
                       branch="main")["status"], registry.CLAIMED)
        self.assertEqual(len(registry.list_active_claims(self.conn)), 2)

    def test_git_identity_is_shared_across_linked_worktrees(self) -> None:
        linked = self.root / "linked"
        run_git(self.repo, "worktree", "add", "-b", "feature", str(linked))
        identity = git.common_dir(str(self.repo))

        proposed = registry.propose(self.conn, "run", "a", str(linked),
                                    paths=["src"])
        self.assertEqual(proposed["repo"], identity)
        self.assertEqual(proposed["repo"], git.common_dir(str(linked)))

        # A claim in one checkout occupies the repository for the other.
        registry.propose(self.conn, "run", "b", str(self.repo), paths=["src"])
        self.assertEqual(
            self.claim("a", "alice", worktree=str(linked),
                       branch="feature")["status"], registry.CLAIMED)
        self.assertEqual(
            self.claim("b", "bob", worktree=str(self.repo),
                       branch="main")["status"], registry.CONFLICT)


class UnknownOccupancyTests(RegistryTestCase):
    def test_missing_observation_refuses_write(self) -> None:
        self.propose("a", ["src"])
        result = self.claim("a", "alice", observation=None,
                            worktree=str(self.repo), branch="main")
        self.assertEqual(result["status"], registry.NEEDS_RECONCILIATION)
        self.assertFalse(result["ok"])

    def test_unavailable_observation_refuses_write(self) -> None:
        self.propose("a", ["src"])
        result = self.claim(
            "a", "alice", worktree=str(self.repo), branch="main",
            observation=RuntimeObservation.unavailable("no snapshot"))
        self.assertEqual(result["status"], registry.NEEDS_RECONCILIATION)

    def test_unknown_agent_state_leaves_occupancy_unknown(self) -> None:
        self.propose("a", ["src"])
        observation = RuntimeObservation(
            True, (AgentObservation("a1", str(self.repo), "not-a-state"),))
        result = self.claim("a", "alice", observation=observation,
                            worktree=str(self.repo), branch="main")
        self.assertEqual(result["status"], registry.NEEDS_RECONCILIATION)

    def test_read_claim_needs_no_observation(self) -> None:
        self.propose("a", ["src"])
        result = self.claim("a", "reader", mode="READ", observation=None)
        self.assertEqual(result["status"], registry.CLAIMED)


class UnregisteredWorkTests(RegistryTestCase):
    def test_dirty_unclaimed_worktree_is_discovered(self) -> None:
        (self.repo / "new.txt").write_text("work\n", encoding="utf-8")
        report = registry.reconcile(self.conn, self.evidence())
        kinds = [f["kind"] for f in report["findings"]]
        self.assertIn(registry.DISCOVERED_UNREGISTERED, kinds)
        finding = next(f for f in report["findings"]
                       if f["kind"] == registry.DISCOVERED_UNREGISTERED)
        self.assertEqual(finding["changes"], ["new.txt"])

    def test_dirty_work_blocks_a_write_claim(self) -> None:
        (self.repo / "new.txt").write_text("work\n", encoding="utf-8")
        self.propose("a", ["new.txt"])
        result = self.claim("a", "alice", worktree=str(self.repo),
                            branch="main")
        self.assertEqual(result["status"], registry.CONFLICT)
        self.assertEqual(result["conflicts"][0]["kind"],
                         "discovered_unregistered")

    def test_clean_quiet_worktree_is_not_discovery(self) -> None:
        report = registry.reconcile(self.conn, self.evidence())
        self.assertEqual(report["findings"], [])


class DriftTests(RegistryTestCase):
    def test_stale_claim_with_changes_blocks_a_writer(self) -> None:
        self.propose("a", ["src"])
        self.assertEqual(
            self.claim("a", "alice", worktree=str(self.repo),
                       branch="main")["status"],
            registry.CLAIMED)
        (self.repo / "src" / "app.py").write_text("print('two')\n",
                                                  encoding="utf-8")

        report = registry.reconcile(self.conn, self.evidence())
        stale = [f for f in report["findings"]
                 if f["kind"] == registry.STALE_DIRTY and f.get("claim_id")]
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["changes"], ["src/app.py"])

        self.propose("b", ["src/app.py"])
        refusal = registry.write_gate(
            self.conn,
            ClaimSpec(run_id="run", work_key="b", owner="bob", mode="WRITE",
                      worktree=str(self.repo), branch="main"),
            self.evidence())
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal["status"], registry.CONFLICT)
        self.assertEqual(refusal["conflicts"][0]["kind"], "stale_dirty")

    def test_scope_drift_outside_declared_paths(self) -> None:
        self.propose("a", ["src"])
        self.claim("a", "alice", worktree=str(self.repo), branch="main")
        (self.repo / "loose.txt").write_text("loose\n", encoding="utf-8")

        report = registry.reconcile(self.conn, self.evidence())
        drift = [f for f in report["findings"]
                 if f["kind"] == registry.SCOPE_DRIFT and f.get("claim_id")]
        self.assertEqual(len(drift), 1)
        self.assertEqual(drift[0]["escaped"], ["loose.txt"])

        self.propose("b", ["src"])
        refusal = registry.write_gate(
            self.conn,
            ClaimSpec(run_id="run", work_key="b", owner="bob", mode="WRITE",
                      worktree=str(self.repo), branch="main"),
            self.evidence())
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal["conflicts"][0]["kind"], "scope_drift")

    def test_committed_rename_out_of_unclaimed_area_is_drift(self) -> None:
        # Renaming a tracked file FROM outside the claimed scope INTO it touches
        # the source endpoint. Counting both endpoints is what makes the move
        # visible: the destination alone sits inside scope and looks contained.
        self.propose("a", ["src"])
        self.claim("a", "alice", worktree=str(self.repo), branch="main")
        run_git(self.repo, "mv", "outside.txt", "src/outside.txt")

        report = registry.reconcile(self.conn, self.evidence())
        drift = [f for f in report["findings"]
                 if f["kind"] == registry.SCOPE_DRIFT and f.get("claim_id")]
        self.assertEqual(len(drift), 1)
        self.assertIn("outside.txt", drift[0]["escaped"])

    def test_branch_drift_fails_closed(self) -> None:
        self.propose("a", ["src"])
        self.assertEqual(
            self.claim("a", "alice", worktree=str(self.repo),
                       branch="main")["status"],
            registry.CLAIMED)
        run_git(self.repo, "checkout", "-b", "feature")

        report = registry.reconcile(self.conn, self.evidence())
        drifted = [f for f in report["findings"] if f.get("branch_drift")]
        self.assertEqual(len(drifted), 1)

        self.propose("b", ["src"])
        refusal = registry.write_gate(
            self.conn,
            ClaimSpec(run_id="run", work_key="b", owner="bob", mode="WRITE",
                      worktree=str(self.repo), branch="main"),
            self.evidence())
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal["conflicts"][0]["kind"], "branch_drift")

    def test_candidate_branch_must_match_the_checkout(self) -> None:
        self.propose("a", ["src"])
        result = self.claim("a", "alice", worktree=str(self.repo),
                            branch="feature")
        self.assertEqual(result["status"], registry.CONFLICT)
        self.assertEqual(result["conflicts"][0]["kind"], "candidate_branch")

    def test_unreadable_status_is_unknown_not_stale_clean(self) -> None:
        self.propose("a", ["src"])
        self.claim("a", "alice", worktree=str(self.repo), branch="main")
        broken = {"ok": False, "source": "git", "reason": "probe_failed",
                  "error": "simulated read failure", "items": None}
        with mock.patch.object(registry.git, "probe_status",
                               return_value=broken):
            report = registry.reconcile(self.conn, self.evidence())
        claim_findings = [f for f in report["findings"] if f.get("claim_id")]
        self.assertEqual(len(claim_findings), 1)
        self.assertEqual(claim_findings[0]["kind"], registry.UNKNOWN)
        self.assertTrue(claim_findings[0].get("unverifiable"))


class HandoffTests(RegistryTestCase):
    def test_handoff_moves_claim_atomically(self) -> None:
        self.propose("a", ["src"])
        self.claim("a", "alice", worktree=str(self.repo), branch="main")

        result = registry.handoff(self.conn, "run", "a", "alice", "bob",
                                  "supervisor", rationale="shift change",
                                  observation=EMPTY)
        self.assertEqual(result["status"], registry.HANDED_OFF)
        self.assertIsNone(registry.get_open_claim(self.conn, "run", "a", "",
                                                  "alice"))
        holder = registry.get_open_claim(self.conn, "run", "a", "", "bob")
        self.assertIsNotNone(holder)
        self.assertEqual(holder["mode"], "WRITE")

        audits = [o for o in registry.list_observations(self.conn, "run", "a")
                  if o["kind"] == "HANDOFF"]
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["payload"]["from_owner"], "alice")
        self.assertEqual(audits[0]["payload"]["to_owner"], "bob")

    def test_handoff_of_missing_claim_refuses_without_changes(self) -> None:
        self.propose("a", ["src"])
        result = registry.handoff(self.conn, "run", "a", "nobody", "bob",
                                  "supervisor")
        self.assertEqual(result["status"], registry.ERROR)
        self.assertEqual(result["released"], [])

    def test_handoff_requires_accountable_authority(self) -> None:
        self.propose("a", ["src"])
        self.claim("a", "alice", worktree=str(self.repo), branch="main")
        with self.assertRaises(RegistryError):
            registry.handoff(self.conn, "run", "a", "alice", "bob", "")
        with self.assertRaises(RegistryError):
            registry.handoff(self.conn, "run", "a", "alice", "alice",
                             "supervisor")

    def test_writing_handoff_without_observation_refuses(self) -> None:
        self.propose("a", ["src"])
        self.claim("a", "alice", worktree=str(self.repo), branch="main")
        result = registry.handoff(self.conn, "run", "a", "alice", "bob",
                                  "supervisor")
        self.assertEqual(result["status"], registry.NEEDS_RECONCILIATION)
        self.assertFalse(result["ok"])
        self.assertIsNotNone(registry.get_open_claim(self.conn, "run", "a", "",
                                                     "alice"))
        self.assertIsNone(registry.get_open_claim(self.conn, "run", "a", "",
                                                  "bob"))

    def test_reading_handoff_needs_no_observation(self) -> None:
        self.propose("a", ["src"])
        self.claim("a", "reader", mode="READ", observation=None)
        result = registry.handoff(self.conn, "run", "a", "reader", "bob",
                                  "supervisor")
        self.assertEqual(result["status"], registry.HANDED_OFF)
        self.assertIsNotNone(registry.get_open_claim(self.conn, "run", "a", "",
                                                     "bob"))

    def test_handoff_branch_drift_refuses_and_keeps_incumbent(self) -> None:
        self.propose("a", ["src"])
        self.claim("a", "alice", worktree=str(self.repo), branch="main")
        run_git(self.repo, "checkout", "-b", "feature")
        result = registry.handoff(self.conn, "run", "a", "alice", "bob",
                                  "supervisor", observation=EMPTY)
        self.assertEqual(result["status"], registry.NEEDS_RECONCILIATION)
        self.assertIsNotNone(registry.get_open_claim(self.conn, "run", "a", "",
                                                     "alice"))
        self.assertIsNone(registry.get_open_claim(self.conn, "run", "a", "",
                                                  "bob"))

    def test_handoff_audit_failure_rolls_back(self) -> None:
        self.propose("a", ["src"])
        self.claim("a", "alice", worktree=str(self.repo), branch="main")
        with mock.patch.object(registry, "record_observation",
                               side_effect=RuntimeError("audit down")):
            with self.assertRaises(RuntimeError):
                registry.handoff(self.conn, "run", "a", "alice", "bob",
                                 "supervisor", observation=EMPTY)
        # The release and the acquire rolled back together; the incumbent holds.
        self.assertIsNotNone(registry.get_open_claim(self.conn, "run", "a", "",
                                                     "alice"))
        self.assertIsNone(registry.get_open_claim(self.conn, "run", "a", "",
                                                  "bob"))


class BindingTests(RegistryTestCase):
    def _assert_refused(self, result: dict) -> None:
        self.assertFalse(result["ok"])
        self.assertIn(result["status"], (registry.NEEDS_SCOPE,
                                         registry.NEEDS_RECONCILIATION))

    def test_write_requires_branch_and_worktree(self) -> None:
        self.propose("a", ["src"])
        self._assert_refused(self.claim("a", "alice", branch="main"))
        self._assert_refused(self.claim("a", "alice", worktree=str(self.repo)))
        self.assertEqual(registry.list_active_claims(self.conn), [])

    def test_exclusive_requires_branch_and_worktree(self) -> None:
        self.propose("a", ["src"])
        self._assert_refused(self.claim("a", "alice", mode="EXCLUSIVE",
                                        branch="main"))
        self._assert_refused(self.claim("a", "alice", mode="EXCLUSIVE",
                                        worktree=str(self.repo)))
        self.assertEqual(registry.list_active_claims(self.conn), [])

    def test_bound_writer_modes_are_accepted(self) -> None:
        self.propose("a", ["src"])
        self.assertEqual(
            self.claim("a", "alice", worktree=str(self.repo),
                       branch="main")["status"], registry.CLAIMED)
        registry.release(self.conn, "run", "alice", "a", outcome="abandon")
        self.assertEqual(
            self.claim("a", "alice", mode="EXCLUSIVE",
                       worktree=str(self.repo),
                       branch="main")["status"], registry.CLAIMED)

    def test_preflight_refuses_unbound_writer(self) -> None:
        self.propose("a", ["src"])
        result = registry.preflight(
            self.conn,
            ClaimSpec(run_id="run", work_key="a", owner="alice", mode="WRITE"),
            EMPTY)
        self._assert_refused(result)

    def test_resume_of_unbound_writer_is_refused(self) -> None:
        self.propose("a", ["src"])
        self.conn.execute(
            "INSERT INTO claims (run_id, work_key, issue, owner, mode, branch, "
            "worktree, claimed_at) VALUES ('run','a','','legacy','WRITE','',"
            "'','t')")
        result = self.claim("a", "legacy")
        self._assert_refused(result)
        self.assertNotEqual(result["status"], registry.RESUMED)

    def test_legacy_unbound_writer_is_unverifiable_and_untransferable(self) -> None:
        self.propose("a", ["src"])
        # Simulate a row written before binding was required.
        self.conn.execute(
            "INSERT INTO claims (run_id, work_key, issue, owner, mode, branch, "
            "worktree, claimed_at) VALUES ('run','a','','legacy','WRITE','',"
            "'','t')")
        report = registry.reconcile(self.conn, self.evidence())
        legacy = next(f for f in report["findings"]
                      if f.get("owner") == "legacy")
        self.assertTrue(legacy.get("unverifiable"))
        self.assertEqual(legacy["kind"], registry.UNKNOWN)

        result = registry.handoff(self.conn, "run", "a", "legacy", "bob",
                                  "supervisor", observation=EMPTY)
        self.assertFalse(result["ok"])
        self.assertIsNotNone(registry.get_open_claim(self.conn, "run", "a", "",
                                                     "legacy"))
        self.assertIsNone(registry.get_open_claim(self.conn, "run", "a", "",
                                                  "bob"))


    def _insert_legacy(self, work_key: str, mode: str, branch: str,
                       worktree: str, owner: str = "legacy") -> None:
        self.propose(work_key, ["src"])
        self.conn.execute(
            "INSERT INTO claims (run_id, work_key, issue, owner, mode, branch, "
            "worktree, claimed_at) VALUES ('run',?,'',?,?,?,?,'t')",
            (work_key, owner, mode, branch, worktree))

    def test_legacy_writer_missing_either_binding_is_unverifiable(self) -> None:
        cases = [
            ("WRITE", "", str(self.repo)),   # branch only omitted
            ("WRITE", "main", ""),           # worktree only omitted
            ("WRITE", "", ""),               # both omitted
            ("EXCLUSIVE", "", str(self.repo)),
            ("EXCLUSIVE", "main", ""),
            ("EXCLUSIVE", "", ""),
        ]
        for i, (mode, branch, worktree) in enumerate(cases):
            self._insert_legacy("w%d" % i, mode, branch, worktree)
        report = registry.reconcile(self.conn, self.evidence())
        legacy = [f for f in report["findings"] if f.get("owner") == "legacy"]
        self.assertEqual(len(legacy), len(cases))
        for finding in legacy:
            self.assertEqual(finding["kind"], registry.UNKNOWN)
            self.assertTrue(finding.get("unverifiable"))

    def test_legacy_writer_with_occupant_stays_unknown(self) -> None:
        cases = [
            ("WRITE", "", str(self.repo)),
            ("WRITE", "main", ""),
            ("WRITE", "", ""),
            ("EXCLUSIVE", "", str(self.repo)),
            ("EXCLUSIVE", "main", ""),
            ("EXCLUSIVE", "", ""),
        ]
        for i, (mode, branch, worktree) in enumerate(cases):
            self._insert_legacy("w%d" % i, mode, branch, worktree)
        observation = RuntimeObservation(
            True, (AgentObservation("a1", str(self.repo), "working"),))
        evidence = registry.gather_evidence(str(self.repo), observation)
        report = registry.reconcile(self.conn, evidence)
        legacy = [f for f in report["findings"] if f.get("owner") == "legacy"]
        self.assertEqual(len(legacy), len(cases))
        for finding in legacy:
            self.assertEqual(finding["kind"], registry.UNKNOWN)
            self.assertTrue(finding.get("unverifiable"))


class ReadOnlyAndReleaseTests(RegistryTestCase):
    def test_read_does_not_create_the_database(self) -> None:
        absent = str(self.root / "absent" / "registry.sqlite3")
        with self.assertRaises(RegistryError):
            registry.connect_readonly(absent)
        self.assertFalse(os.path.exists(absent))

    def test_read_only_connection_opens_existing_and_cannot_write(self) -> None:
        self.propose("a", ["src"])
        self.conn.close()
        self.assertTrue(os.path.isfile(self.db))

        ro = registry.connect_readonly(self.db)
        self.addCleanup(ro.close)
        self.assertEqual(registry.list_active_claims(ro), [])
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("INSERT INTO runs (run_id, created_at) VALUES ('x','y')")

    def test_corrupt_observation_payload_raises(self) -> None:
        for payload in ("{not json", "NaN", '{"x": NaN}', "[1, 2]", "null"):
            with self.subTest(payload=payload):
                self.conn.execute("DELETE FROM observations")
                self.conn.execute(
                    "INSERT INTO observations (run_id, work_key, kind, payload, "
                    "observed_at) VALUES ('run','','RECONCILIATION',?,'t')",
                    (payload,))
                with self.assertRaises(registry.RegistryCorruptError):
                    registry.list_observations(self.conn, "run")

    def test_corrupt_observation_error_leaks_nothing(self) -> None:
        secret = "SECRET-PAYLOAD-VALUE"
        self.conn.execute(
            "INSERT INTO observations (run_id, work_key, kind, payload, "
            "observed_at) VALUES ('run','','RECONCILIATION',?,'t')",
            ("{not json %s" % secret,))
        with self.assertRaises(registry.RegistryCorruptError) as ctx:
            registry.list_observations(self.conn, "run")
        error = ctx.exception
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertNotIn(secret, str(error))

    def test_record_observation_rejects_bad_payloads(self) -> None:
        with self.assertRaises(registry.RegistryError):
            registry.record_observation(self.conn, "run", "", "K",
                                        {"x": float("nan")})
        with self.assertRaises(registry.RegistryError):
            registry.record_observation(self.conn, "run", "", "K", [1, 2])

    def test_release_is_terminal_and_reclaim_is_fresh(self) -> None:
        self.propose("a", ["src"])
        first = self.claim("a", "alice", worktree=str(self.repo),
                           branch="main")
        result = registry.release(self.conn, "run", "alice", "a",
                                  outcome="complete", released_head="a" * 40)
        self.assertEqual(result["status"], registry.RELEASED)
        self.assertEqual(result["released"], [first["claim_id"]])
        self.assertIsNone(registry.get_open_claim(self.conn, "run", "a", "",
                                                  "alice"))
        again = self.claim("a", "alice", worktree=str(self.repo),
                           branch="main")
        self.assertEqual(again["status"], registry.CLAIMED)
        self.assertNotEqual(again["claim_id"], first["claim_id"])

    def test_released_head_requires_complete_and_exact_sha(self) -> None:
        self.propose("a", ["src"])
        self.claim("a", "alice", worktree=str(self.repo), branch="main")
        with self.assertRaises(RegistryError):
            registry.release(self.conn, "run", "alice", "a", outcome="abandon",
                             released_head="a" * 40)
        with self.assertRaises(RegistryError):
            registry.release(self.conn, "run", "alice", "a", outcome="complete",
                             released_head="not-a-sha")


class DispositionTests(RegistryTestCase):
    def test_historical_decision_is_bound_to_reviewed_evidence(self) -> None:
        self.propose("a", ["src"])
        self.claim("a", "alice", worktree=str(self.repo), branch="main")
        (self.repo / "src" / "app.py").write_text("print('two')\n",
                                                  encoding="utf-8")
        report = registry.reconcile(self.conn, self.evidence())
        finding = next(f for f in report["findings"]
                       if f["kind"] == registry.STALE_DIRTY)

        # A decision quoting the wrong reviewed fingerprint is refused.
        with self.assertRaises(RegistryError):
            registry.record_disposition(
                self.conn, "run", finding, "historical", "supervisor",
                "finished elsewhere", expected_fingerprint="deadbeef")

        registry.record_disposition(
            self.conn, "run", finding, "historical", "supervisor",
            "finished elsewhere",
            expected_fingerprint=finding["fingerprint"])
        self.assertIsNotNone(
            registry.find_disposition(self.conn, "run", finding))

        self.propose("b", ["src/app.py"])
        cleared = registry.write_gate(
            self.conn,
            ClaimSpec(run_id="run", work_key="b", owner="bob", mode="WRITE",
                      worktree=str(self.repo), branch="main"),
            self.evidence())
        self.assertIsNone(cleared)

        # A different edit changes the evidence, so the decision stops applying.
        (self.repo / "src" / "app.py").write_text("print('three')\n",
                                                  encoding="utf-8")
        new_evidence = self.evidence()
        report2 = registry.reconcile(self.conn, new_evidence)
        changed = next(f for f in report2["findings"]
                       if f["kind"] == registry.STALE_DIRTY)
        self.assertIsNone(
            registry.find_disposition(self.conn, "run", changed))
        refused = registry.write_gate(
            self.conn,
            ClaimSpec(run_id="run", work_key="b", owner="bob", mode="WRITE",
                      worktree=str(self.repo), branch="main"),
            new_evidence)
        self.assertIsNotNone(refused)

    def test_unknown_findings_cannot_be_dispositioned(self) -> None:
        unknown = {"kind": registry.UNKNOWN, "worktree": str(self.repo)}
        registry.stamp_findings([unknown])
        with self.assertRaises(RegistryError):
            registry.record_disposition(self.conn, "run", unknown, "historical",
                                        "supervisor", "note")


class AdvisorySimilarityTests(RegistryTestCase):
    def test_overlap_scan_is_advisory_and_changes_nothing(self) -> None:
        self.propose("a", ["src"],
                     objective="portable work registry with git evidence")
        self.propose("b", ["lib"],
                     objective="portable git evidence work registry")
        # One owner, one checkout, one branch, disjoint paths: both bind.
        self.claim("a", "alice", worktree=str(self.repo), branch="main")
        self.claim("b", "alice", worktree=str(self.repo), branch="main")
        before = len(registry.list_active_claims(self.conn))

        identity = git.common_dir(str(self.repo))
        pairs = registry.probable_overlaps(self.conn, identity)
        self.assertTrue(pairs)
        self.assertTrue(pairs[0]["advisory"])
        self.assertEqual(len(registry.list_active_claims(self.conn)), before)


class ValidationTests(RegistryTestCase):
    def test_rejects_absolute_and_escaping_scope(self) -> None:
        with self.assertRaises(RegistryError):
            self.propose("a", ["/etc/passwd"])
        with self.assertRaises(RegistryError):
            self.propose("a", ["../outside"])
        with self.assertRaises(RegistryError):
            self.propose("a", [])

    def test_resume_requires_identical_identity(self) -> None:
        self.propose("a", ["src"])
        first = self.claim("a", "alice", worktree=str(self.repo),
                           branch="main")
        self.assertEqual(first["status"], registry.CLAIMED)
        resumed = self.claim("a", "alice", worktree=str(self.repo),
                             branch="main")
        self.assertEqual(resumed["status"], registry.RESUMED)
        # A resume that drops the declared binding is a different identity and
        # is refused rather than silently taking the claim over.
        mismatch = self.claim("a", "alice", worktree=str(self.repo))
        self.assertEqual(mismatch["status"], registry.ERROR)


class FingerprintTests(RegistryTestCase):
    def test_finding_fingerprint_rejects_non_finite(self) -> None:
        for finding in (
                {"kind": registry.LIVE, "scope": [float("nan")]},
                {"kind": registry.LIVE, "dirty_digest": float("inf")},
                {"kind": registry.LIVE,
                 "workers": [{"agent_id": float("nan")}]}):
            with self.subTest(finding=finding):
                with self.assertRaises(registry.RegistryError):
                    registry.finding_fingerprint(finding)


class EvidenceValidityTests(RegistryTestCase):
    def test_unavailable_worktrees_are_not_an_empty_reading(self) -> None:
        evidence = {
            "ok": False,
            "repo": str(self.repo),
            "identity": "",
            "worktrees": {"ok": False, "reason": "probe_failed",
                          "error": "git worktree list could not be read",
                          "items": None},
            "baseline": "",
            "observation": EMPTY,
            "errors": ["git worktree: unavailable"],
        }
        report = registry.reconcile(self.conn, evidence)
        self.assertFalse(report["ok"])
        self.assertEqual(report["findings"], [])
        self.assertTrue(report["errors"])

    def test_gather_evidence_propagates_baseline_failure(self) -> None:
        good_wt = {"ok": True, "source": "git", "reason": "", "error": "",
                                       "items": [{"path": str(self.repo),
                                          "head": "a" * 40,
                                          "branch": "main",
                                          "detached": False,
                                          "bare": False,
                                          "prunable": False,
                                          "locked": False}]}
        bad_base = {"ok": False, "reason": "probe_failed",
                    "error": "git baseline could not be read", "items": None}
        with mock.patch.object(registry.git, "probe_worktrees",
                               return_value=good_wt), \
                mock.patch.object(registry.git, "probe_baseline_ref",
                                  return_value=bad_base):
            evidence = registry.gather_evidence(str(self.repo), EMPTY)
        self.assertFalse(evidence["ok"])
        self.assertTrue(evidence["errors"])
        self.assertEqual(evidence["baseline"], "")
        self.assertIsNone(evidence["baseline_probe"]["items"])

    def test_gather_evidence_propagates_worktree_failure(self) -> None:
        bad_wt = {"ok": False, "reason": "probe_failed",
                  "error": "git worktree list could not be read", "items": None}
        bad_base = {"ok": False, "reason": "probe_failed",
                    "error": "git baseline could not be read", "items": None}
        with mock.patch.object(registry.git, "probe_worktrees",
                               return_value=bad_wt), \
                mock.patch.object(registry.git, "probe_baseline_ref",
                                  return_value=bad_base):
            evidence = registry.gather_evidence(str(self.repo), EMPTY)
        self.assertFalse(evidence["ok"])
        self.assertIsNone(evidence["worktrees"]["items"])
        # No fabricated ok=True empty reading.
        self.assertFalse(evidence["baseline_probe"]["ok"])

    def test_write_refused_when_baseline_probe_fails(self) -> None:
        self.propose("a", ["src"])
        bad_base = {"ok": False, "reason": "probe_failed",
                    "error": "git baseline could not be read", "items": None}
        with mock.patch.object(registry.git, "probe_baseline_ref",
                               return_value=bad_base):
            result = self.claim("a", "alice", worktree=str(self.repo),
                                branch="main")
        self.assertEqual(result["status"], registry.NEEDS_RECONCILIATION)
        self.assertEqual(registry.list_active_claims(self.conn), [])

    def _good_worktrees(self) -> dict:
        return {"ok": True, "source": "git", "reason": "", "error": "",
                                       "items": [{"path": str(self.repo),
                                       "head": "a" * 40,
                                       "branch": "main",
                                       "detached": False,
                                       "bare": False,
                                       "prunable": False,
                                       "locked": False}]}

    def test_evidence_requires_correct_probe_shape(self) -> None:
        healthy_baseline = {"ok": True, "source": "git", "reason": "",
                            "error": "", "items": ["main"]}
        # A worktree probe needs ok AND a nonempty items list.
        for probe in ({"ok": True, "items": None},
                      {"ok": True, "items": []},
                      {"ok": True},
                      {"ok": True, "items": "x"}):
            with self.subTest(worktrees=probe):
                with mock.patch.object(registry.git, "probe_worktrees",
                                       return_value=probe), \
                        mock.patch.object(registry.git, "probe_baseline_ref",
                                          return_value=healthy_baseline):
                    evidence = registry.gather_evidence(str(self.repo), EMPTY)
                self.assertFalse(evidence["ok"])
        # A baseline probe needs ok AND an items list (empty is legitimate).
        for probe in ({"ok": True, "items": None}, {"ok": True},
                      {"ok": True, "items": "x"}):
            with self.subTest(baseline=probe):
                with mock.patch.object(registry.git, "probe_worktrees",
                                       return_value=self._good_worktrees()), \
                        mock.patch.object(registry.git, "probe_baseline_ref",
                                          return_value=probe):
                    evidence = registry.gather_evidence(str(self.repo), EMPTY)
                self.assertFalse(evidence["ok"])
        # ... but a well-shaped, genuinely-empty baseline is healthy.
        with mock.patch.object(registry.git, "probe_worktrees",
                               return_value=self._good_worktrees()), \
                mock.patch.object(registry.git, "probe_baseline_ref",
                                  return_value={"ok": True, "source": "git", "reason": "",
                                  "error": "", "items": []}):
            evidence = registry.gather_evidence(str(self.repo), EMPTY)
        self.assertTrue(evidence["ok"])

    def test_reconcile_rejects_malformed_probe_shapes(self) -> None:
        base = {"ok": True, "items": []}
        for worktrees in ({"ok": True, "items": None},
                          {"ok": True, "items": []},
                          {"ok": True}):
            with self.subTest(worktrees=worktrees):
                report = registry.reconcile(self.conn, {
                    "ok": True, "repo": str(self.repo), "identity": "",
                    "worktrees": worktrees, "baseline_probe": base,
                    "observation": EMPTY, "errors": []})
                self.assertFalse(report["ok"])
                self.assertEqual(report["findings"], [])
        for baseline in ({"ok": True, "items": None}, {"ok": True},
                         {"ok": False, "items": None}, {}):
            with self.subTest(baseline=baseline):
                report = registry.reconcile(self.conn, {
                    "ok": True, "repo": str(self.repo), "identity": "",
                    "worktrees": self._good_worktrees(),
                    "baseline_probe": baseline,
                    "observation": EMPTY, "errors": []})
                self.assertFalse(report["ok"])
                self.assertEqual(report["findings"], [])

    def test_corrupt_observation_traceback_has_no_raw_secret(self) -> None:
        secret = "SECRET-PAYLOAD-VALUE"
        self.conn.execute(
            "INSERT INTO observations (run_id, work_key, kind, payload, "
            "observed_at) VALUES ('run','','K',?,'t')",
            ("{not json %s" % secret,))
        try:
            registry.list_observations(self.conn, "run")
        except registry.RegistryCorruptError as exc:
            frames = []
            tb = exc.__traceback__
            while tb is not None:
                # Only library frames matter; the caller's frame legitimately
                # holds the argument it passed in.
                if tb.tb_frame.f_code.co_filename.rsplit("/", 1)[-1] == "registry.py":
                    frames.append(tb.tb_frame.f_locals)
                tb = tb.tb_next
            blob = " ".join(repr(sorted(f.items())) for f in frames)
            self.assertIsNone(exc.__cause__)
            self.assertIsNone(exc.__context__)
            self.assertNotIn(secret, blob)
            # The database handle must not be reachable from a library frame
            # either: the row's payload is reachable through the connection.
            self.assertFalse(
                any(value is self.conn for f in frames for value in f.values()))
        else:
            self.fail("expected RegistryCorruptError")

    @staticmethod
    def _probe(items, **extra):
        probe = {"ok": True, "source": "git", "reason": "", "error": "",
                 "items": items}
        probe.update(extra)
        return probe

    def test_worktree_probe_rejects_invalid_content(self) -> None:
        base = {"path": str(self.repo), "head": "a" * 40, "branch": "main",
                "detached": False, "bare": False, "prunable": False,
                "locked": False}
        malformed_items = [
            ("bad head", [dict(base, head="not-a-commit")]),
            ("bad branch", [dict(base, branch="not a ref")]),
            ("escaping path", [dict(base, path="../outside")]),
            ("missing prunable",
             [{k: v for k, v in base.items() if k != "prunable"}]),
            ("duplicate identity", [base, dict(base)]),
        ]
        for label, items in malformed_items:
            with self.subTest(label=label):
                with mock.patch.object(
                        registry.git, "probe_worktrees",
                        return_value=self._probe(items)), \
                        mock.patch.object(registry.git, "probe_baseline_ref",
                                          return_value=self._probe([])):
                    evidence = registry.gather_evidence(str(self.repo), EMPTY)
                self.assertFalse(evidence["ok"])
        # Arbitrary source and non-string falsy reason/error are not healthy.
        for override in ({"source": "evil"}, {"reason": 0}, {"error": 0}):
            with self.subTest(override=override):
                probe = self._probe([base])
                probe.update(override)
                with mock.patch.object(registry.git, "probe_worktrees",
                                       return_value=probe), \
                        mock.patch.object(registry.git, "probe_baseline_ref",
                                          return_value=self._probe([])):
                    evidence = registry.gather_evidence(str(self.repo), EMPTY)
                self.assertFalse(evidence["ok"])
        # Multiple baseline refs are invalid.
        with mock.patch.object(registry.git, "probe_worktrees",
                               return_value=self._probe([base])), \
                mock.patch.object(registry.git, "probe_baseline_ref",
                                  return_value=self._probe(["main", "other"])):
            self.assertFalse(
                registry.gather_evidence(str(self.repo), EMPTY)["ok"])

    def test_reconcile_rejects_invalid_probe_content(self) -> None:
        self.propose("a", ["src"])
        self.claim("a", "alice", worktree=str(self.repo), branch="main")
        evidence = self.evidence()
        secret = "SYNTH_SECRET"
        status_zz = self._probe(
            [{"index": "Z", "worktree": "Z", "path": "x", "orig_path": "",
              "staged": True, "unstaged": True, "untracked": False}],
            worktree=str(self.repo))
        digest_secret = self._probe([], worktree=str(self.repo), clean=False,
                                    digest=secret)
        committed_escape = self._probe(
            ["../outside"], worktree=str(self.repo), base="a" * 40)
        for target, probe in (("probe_status", status_zz),
                              ("probe_dirty_digest", digest_secret),
                              ("probe_committed_diff", committed_escape)):
            with self.subTest(target=target):
                with mock.patch.object(registry.git, target,
                                       return_value=probe):
                    report = registry.reconcile(self.conn, evidence)
                finding = [f for f in report["findings"]
                           if f.get("claim_id")][0]
                self.assertEqual(finding["kind"], registry.UNKNOWN)
                self.assertNotIn(secret, repr(report))

    def test_evidence_with_inconsistent_errors_is_not_ok(self) -> None:
        secret = "SYNTH_SECRET"
        evidence = self.evidence()
        self.assertTrue(evidence["ok"])
        bad = dict(evidence)
        bad["errors"] = [secret]
        report = registry.reconcile(self.conn, bad)
        self.assertFalse(report["ok"])
        self.assertNotIn(secret, repr(report))
        self.propose("a", ["src"])
        spec = ClaimSpec(run_id="run", work_key="a", owner="alice",
                         mode="WRITE", worktree=str(self.repo),
                         branch="main")
        refusal = registry.write_gate(self.conn, spec, bad)
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal["status"], registry.NEEDS_RECONCILIATION)
        self.assertNotIn(secret, repr(refusal))

    def test_runtime_observation_shape_is_validated(self) -> None:
        secret = "SYNTH_SECRET"
        self.assertTrue(
            registry.gather_evidence(str(self.repo), EMPTY)["ok"])
        malformed = [
            RuntimeObservation(1, ()),
            RuntimeObservation(True, None),
            RuntimeObservation(True, (), secret),
            RuntimeObservation(True, ("not-an-agent",)),
        ]
        for obs in malformed:
            with self.subTest(obs=obs):
                evidence = registry.gather_evidence(str(self.repo), obs)
                self.assertFalse(evidence["ok"])
                self.assertNotIn(secret, repr(evidence))
        # A valid agent observation is accepted.
        agent = AgentObservation("a1", str(self.repo), "working")
        good = registry.gather_evidence(
            str(self.repo), RuntimeObservation(True, (agent,)))
        self.assertTrue(good["ok"])

    def _library_frames(self, exc):
        frames = []
        tb = exc.__traceback__
        while tb is not None:
            if tb.tb_frame.f_code.co_filename.rsplit("/", 1)[-1] == "registry.py":
                frames.append(tb.tb_frame.f_locals)
            tb = tb.tb_next
        return frames

    def test_corrupt_row_after_valid_secret_row_leaks_nothing(self) -> None:
        secret = "SECRET-PAYLOAD-VALUE"
        # A VALID observation whose payload legitimately contains the secret,
        # followed by a corrupt one.
        self.conn.execute(
            "INSERT INTO observations (run_id, work_key, kind, payload, "
            "observed_at) VALUES ('run','','K',?,'t')",
            ('{"note": "%s"}' % secret,))
        self.conn.execute(
            "INSERT INTO observations (run_id, work_key, kind, payload, "
            "observed_at) VALUES ('run','','K','{bad','t')")
        try:
            registry.list_observations(self.conn, "run")
        except registry.RegistryCorruptError as exc:
            frames = self._library_frames(exc)
            blob = " ".join(repr(sorted(f.items())) for f in frames)
            self.assertNotIn(secret, blob)
            self.assertFalse(
                any(value is self.conn for f in frames for value in f.values()))
        else:
            self.fail("expected RegistryCorruptError")

    def test_malformed_write_identifiers_not_in_traceback(self) -> None:
        secret = "SECRET-PAYLOAD-VALUE"
        for payload in (secret, {"x": float("nan")}):
            with self.subTest(payload=repr(payload)[:16]):
                try:
                    registry.record_observation(self.conn, secret, secret,
                                                secret, payload)
                except registry.RegistryError as exc:
                    frames = self._library_frames(exc)
                    blob = " ".join(repr(sorted(f.items())) for f in frames)
                    self.assertNotIn(secret, blob)
                    self.assertFalse(
                        any(value is self.conn
                            for f in frames for value in f.values()))
                else:
                    self.fail("expected RegistryError")

    def test_reconcile_rejects_healthy_label_without_shape(self) -> None:
        self.propose("a", ["src"])
        self.claim("a", "alice", worktree=str(self.repo), branch="main")
        evidence = self.evidence()
        bad_statuses = [
            {"ok": True},
            {"ok": True, "items": None},
            {"ok": True, "reason": "probe_failed", "items": []},
            {"ok": True, "items": [{"bogus": "x"}]},
            {"ok": True, "error": "boom", "items": []},
        ]
        for bad in bad_statuses:
            with self.subTest(status=bad):
                with mock.patch.object(registry.git, "probe_status",
                                       return_value=bad):
                    report = registry.reconcile(self.conn, evidence)
                claim_findings = [f for f in report["findings"]
                                  if f.get("claim_id")]
                self.assertEqual(claim_findings[0]["kind"], registry.UNKNOWN)
                self.assertTrue(claim_findings[0].get("unverifiable"))

    def test_reconcile_rejects_ok_true_dirty_digest_and_committed(self) -> None:
        self.propose("a", ["src"])
        self.claim("a", "alice", worktree=str(self.repo), branch="main")
        evidence = self.evidence()
        with mock.patch.object(registry.git, "probe_dirty_digest",
                               return_value={"ok": True}):
            report = registry.reconcile(self.conn, evidence)
        finding = [f for f in report["findings"] if f.get("claim_id")][0]
        self.assertEqual(finding["kind"], registry.UNKNOWN)
        with mock.patch.object(registry.git, "probe_committed_diff",
                               return_value={"ok": True}):
            report = registry.reconcile(self.conn, evidence)
        finding = [f for f in report["findings"] if f.get("claim_id")][0]
        self.assertEqual(finding["kind"], registry.UNKNOWN)

    def test_reconcile_rejects_top_level_ok_false(self) -> None:
        evidence = self.evidence()
        evidence["ok"] = False
        report = registry.reconcile(self.conn, evidence)
        self.assertFalse(report["ok"])
        self.assertTrue(report["errors"])
        # Runtime evidence unavailable but errors empty must still fail closed.
        evidence = self.evidence()
        evidence["observation"] = None
        report = registry.reconcile(self.conn, evidence)
        self.assertFalse(report["ok"])
        self.assertTrue(report["errors"])

    def test_write_gate_rejects_malformed_evidence_shape(self) -> None:
        self.propose("a", ["src"])
        spec = ClaimSpec(run_id="run", work_key="a", owner="alice",
                         mode="WRITE", worktree=str(self.repo), branch="main")
        for worktrees in ({"ok": True, "items": None},
                          {"ok": True, "items": []},
                          {"ok": True}):
            with self.subTest(worktrees=worktrees):
                refusal = registry.write_gate(self.conn, spec, {
                    "ok": True, "repo": str(self.repo), "identity": "",
                    "worktrees": worktrees,
                    "baseline_probe": {"ok": True, "items": []},
                    "observation": EMPTY})
                self.assertIsNotNone(refusal)
                self.assertEqual(refusal["status"],
                                 registry.NEEDS_RECONCILIATION)
        self.assertEqual(registry.list_active_claims(self.conn), [])

    def test_probe_error_fields_are_not_echoed(self) -> None:
        secret = "SECRET-PROBE-DIAGNOSTIC"
        bad = {"ok": False, "source": "git", "reason": secret,
               "error": secret, "items": None}
        with mock.patch.object(registry.git, "probe_worktrees",
                               return_value=bad), \
                mock.patch.object(registry.git, "probe_baseline_ref",
                                  return_value=bad):
            evidence = registry.gather_evidence(str(self.repo), EMPTY)
        self.assertFalse(evidence["ok"])
        self.assertNotIn(secret, repr(evidence))
        report = registry.reconcile(self.conn, {
            "ok": False, "repo": str(self.repo), "identity": "",
            "worktrees": bad, "baseline_probe": bad,
            "observation": EMPTY, "errors": []})
        self.assertFalse(report["ok"])
        self.assertNotIn(secret, repr(report))

    def test_extra_probe_keys_with_secret_are_rejected_and_not_echoed(self) -> None:
        secret = "SECRET-EXTRA-KEY"
        good_item = {"path": str(self.repo), "head": "a" * 40,
                     "branch": "main", "detached": False, "bare": False,
                     "prunable": False, "locked": False}
        probes = [
            {"ok": True, "items": [good_item], "extra": secret},
            {"ok": True, "items": [dict(good_item, leaked=secret)]},
        ]
        for probe in probes:
            with self.subTest(probe=repr(probe)[:32]):
                with mock.patch.object(registry.git, "probe_worktrees",
                                       return_value=probe), \
                        mock.patch.object(registry.git, "probe_baseline_ref",
                                          return_value={"ok": True,
                                                        "items": []}):
                    evidence = registry.gather_evidence(str(self.repo), EMPTY)
                self.assertFalse(evidence["ok"])
                self.assertNotIn(secret, repr(evidence))


    @staticmethod
    def _status_item(index, worktree, path, orig=""):
        return {"index": index, "worktree": worktree, "path": path,
                "orig_path": orig,
                "staged": index not in (" ", "?"),
                "unstaged": worktree not in (" ", "?"),
                "untracked": index == "?" and worktree == "?"}

    @staticmethod
    def _worktree_item():
        return {"path": "", "head": "a" * 40, "branch": "main",
                "detached": False, "bare": False, "prunable": False,
                "locked": False}

    def test_missing_mandatory_probe_keys_fail_closed(self) -> None:
        base = self._worktree_item()
        base["path"] = str(self.repo)
        for missing in ("reason", "error"):
            with self.subTest(probe="worktrees", missing=missing):
                probe = self._probe([base])
                del probe[missing]
                with mock.patch.object(registry.git, "probe_worktrees",
                                       return_value=probe), \
                        mock.patch.object(registry.git, "probe_baseline_ref",
                                          return_value=self._probe([])):
                    self.assertFalse(
                        registry.gather_evidence(str(self.repo), EMPTY)["ok"])
            with self.subTest(probe="baseline", missing=missing):
                probe = self._probe([])
                del probe[missing]
                with mock.patch.object(registry.git, "probe_worktrees",
                                       return_value=self._probe([base])), \
                        mock.patch.object(registry.git, "probe_baseline_ref",
                                          return_value=probe):
                    self.assertFalse(
                        registry.gather_evidence(str(self.repo), EMPTY)["ok"])
        self.propose("a", ["src"])
        self.claim("a", "alice", worktree=str(self.repo), branch="main")
        evidence = self.evidence()
        probes = {
            "probe_status": self._probe([], worktree=str(self.repo)),
            "probe_dirty_digest": self._probe([], worktree=str(self.repo),
                                              clean=True, digest=""),
            "probe_committed_diff": self._probe([], worktree=str(self.repo),
                                                base="a" * 40),
        }
        for target, probe in probes.items():
            for missing in ("reason", "error"):
                with self.subTest(target=target, missing=missing):
                    bad = dict(probe)
                    del bad[missing]
                    with mock.patch.object(registry.git, target,
                                           return_value=bad):
                        report = registry.reconcile(self.conn, evidence)
                    finding = [f for f in report["findings"]
                               if f.get("claim_id")][0]
                    self.assertEqual(finding["kind"], registry.UNKNOWN)

    def test_missing_mandatory_evidence_keys_fail_closed(self) -> None:
        base_evidence = self.evidence()
        for key in ("ok", "errors", "repo", "identity", "worktree",
                    "worktrees", "baseline_probe", "observation"):
            with self.subTest(missing=key):
                bad = dict(base_evidence)
                bad.pop(key, None)
                report = registry.reconcile(self.conn, bad)
                self.assertFalse(report["ok"])
        self.propose("a", ["src"])
        spec = ClaimSpec(run_id="run", work_key="a", owner="alice",
                         mode="WRITE", worktree=str(self.repo),
                         branch="main")
        bad = dict(base_evidence)
        bad.pop("errors", None)
        refusal = registry.write_gate(self.conn, spec, bad)
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal["status"], registry.NEEDS_RECONCILIATION)

    def test_probe_rejects_impossible_git_state(self) -> None:
        base = self._worktree_item()
        base["path"] = str(self.repo)
        impossible = [
            dict(base, detached=True),          # attached AND detached
            dict(base, branch=""),              # non-detached, empty branch
            dict(base, bare=True),              # bare carrying a branch
            dict(base, path="/etc/passwd"),     # not a directory
            dict(base, path=str(self.repo) + "//x"),   # non-canonical
            dict(base, path=str(self.repo) + "/./x"),  # dot segment
            dict(base, path=str(self.repo) + "\x00"),  # NUL byte
        ]
        for item in impossible:
            with self.subTest(item=repr(item)[:48]):
                with mock.patch.object(registry.git, "probe_worktrees",
                                       return_value=self._probe([item])), \
                        mock.patch.object(registry.git, "probe_baseline_ref",
                                          return_value=self._probe([])):
                    self.assertFalse(
                        registry.gather_evidence(str(self.repo), EMPTY)["ok"])
        self.propose("a", ["src"])
        self.claim("a", "alice", worktree=str(self.repo), branch="main")
        evidence = self.evidence()
        contradictory = self._status_item(" ", "M", "x")
        contradictory["staged"] = True
        duplicate = self._status_item(" ", "M", "x")
        bad_statuses = [
            [contradictory],
            [duplicate, dict(duplicate)],
            [self._status_item("R", " ", "same", "same")],
            [self._status_item(" ", "M", "a//b")],
        ]
        for items in bad_statuses:
            with self.subTest(status=repr(items)[:48]):
                probe = self._probe(items, worktree=str(self.repo))
                with mock.patch.object(registry.git, "probe_status",
                                       return_value=probe):
                    report = registry.reconcile(self.conn, evidence)
                finding = [f for f in report["findings"]
                           if f.get("claim_id")][0]
                self.assertEqual(finding["kind"], registry.UNKNOWN)
        for path in ("a//b", "a/\x00b"):
            with self.subTest(committed=repr(path)):
                probe = self._probe([path], worktree=str(self.repo),
                                    base="a" * 40)
                with mock.patch.object(registry.git, "probe_committed_diff",
                                       return_value=probe):
                    report = registry.reconcile(self.conn, evidence)
                finding = [f for f in report["findings"]
                           if f.get("claim_id")][0]
                self.assertEqual(finding["kind"], registry.UNKNOWN)

    def test_caller_supplied_identity_and_warning_not_exposed(self) -> None:
        secret = "SYNTH_SECRET"
        evidence = self.evidence()
        tainted = dict(evidence)
        tainted["repo"] = secret
        tainted["identity"] = secret
        tainted["worktree"] = secret
        report = registry.reconcile(self.conn, tainted)
        self.assertNotIn(secret, repr(report))
        # A baseline ref must not be interpolated into a public warning.
        self.assertNotIn(
            secret, " ".join(git.baseline_warnings(str(self.repo), secret)))
        with mock.patch.object(registry.git, "probe_baseline_ref",
                               return_value=self._probe([secret])):
            gathered = registry.gather_evidence(str(self.repo), EMPTY)
        self.assertNotIn(secret, repr(gathered["baseline_warnings"]))

    def test_hostile_containers_fail_closed_not_propagate(self) -> None:
        secret = "SYNTH_SECRET"

        class BoomDict(dict):
            def __getitem__(self, key):
                raise RuntimeError(secret)

            def get(self, *args, **kwargs):
                raise RuntimeError(secret)

            def __contains__(self, key):
                raise RuntimeError(secret)

            def __iter__(self):
                raise RuntimeError(secret)

            def keys(self):
                raise RuntimeError(secret)

        class BoomList(list):
            def __iter__(self):
                raise RuntimeError(secret)

            def __len__(self):
                raise RuntimeError(secret)

        class BoomTuple(tuple):
            def __iter__(self):
                raise RuntimeError(secret)

        report = registry.reconcile(self.conn, BoomDict())
        self.assertFalse(report["ok"])
        self.assertNotIn(secret, repr(report))

        self.propose("a", ["src"])
        spec = ClaimSpec(run_id="run", work_key="a", owner="alice",
                         mode="WRITE", worktree=str(self.repo),
                         branch="main")
        refusal = registry.write_gate(self.conn, spec, BoomDict())
        self.assertIsNotNone(refusal)
        self.assertNotIn(secret, repr(refusal))

        evidence = registry.gather_evidence(
            str(self.repo), RuntimeObservation(True, BoomTuple(())))
        self.assertFalse(evidence["ok"])
        self.assertNotIn(secret, repr(evidence))

        hostile_probe = {"ok": True, "source": "git", "reason": "", "error": "",
                         "items": BoomList(self._good_worktrees()["items"])}
        tainted = dict(self.evidence())
        tainted["worktrees"] = hostile_probe
        hostile_report = registry.reconcile(self.conn, tainted)
        self.assertFalse(hostile_report["ok"])
        self.assertNotIn(secret, repr(hostile_report))


    def test_write_gate_refuses_missing_or_invalid_identity(self) -> None:
        self.propose("a", ["src"])
        spec = ClaimSpec(run_id="run", work_key="a", owner="alice",
                         mode="WRITE", worktree=str(self.repo),
                         branch="main")
        evidence = self.evidence()
        for field, value in (("identity", ""), ("identity", "/no/such/path"),
                             ("repo", ""), ("repo", "/no/such/path"),
                             ("worktree", "/no/such/path")):
            with self.subTest(field=field, value=value):
                bad = dict(evidence)
                bad[field] = value
                refusal = registry.write_gate(self.conn, spec, bad)
                self.assertIsNotNone(refusal)
                self.assertEqual(refusal["status"],
                                 registry.NEEDS_RECONCILIATION)
        # A different (existing) worktree is not the declared binding.
        other = tempfile.mkdtemp()
        bad = dict(evidence)
        bad["worktree"] = other
        refusal = registry.write_gate(self.conn, spec, bad)
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal["status"], registry.NEEDS_RECONCILIATION)
        self.assertEqual(registry.list_active_claims(self.conn), [])

    def test_validator_returns_plain_copies_and_rejects_subclasses(self) -> None:
        class Sneaky(str):
            pass

        base = self._worktree_item()
        base["path"] = str(self.repo)
        sneaky = dict(base, head=Sneaky("a" * 40))
        with mock.patch.object(registry.git, "probe_worktrees",
                               return_value=self._probe([sneaky])), \
                mock.patch.object(registry.git, "probe_baseline_ref",
                                  return_value=self._probe([])):
            self.assertFalse(
                registry.gather_evidence(str(self.repo), EMPTY)["ok"])
        # A valid probe yields canonical PLAIN builtins, not the caller object.
        good = self._worktree_item()
        good["path"] = str(self.repo)
        with mock.patch.object(registry.git, "probe_worktrees",
                               return_value=self._probe([good])), \
                mock.patch.object(registry.git, "probe_baseline_ref",
                                  return_value=self._probe([])):
            evidence = registry.gather_evidence(str(self.repo), EMPTY)
        self.assertTrue(evidence["ok"])
        self.assertIs(type(evidence["worktrees"]), dict)
        self.assertIs(type(evidence["worktrees"]["items"]), list)
        self.assertIs(type(evidence["worktrees"]["items"][0]), dict)
        self.assertIs(type(evidence["worktrees"]["items"][0]["head"]), str)

    def test_validator_rejects_extra_probe_and_aggregate_keys(self) -> None:
        self.propose("a", ["src"])
        self.claim("a", "alice", worktree=str(self.repo), branch="main")
        evidence = self.evidence()
        digest = {"ok": True, "source": "git", "reason": "", "error": "",
                  "worktree": str(self.repo), "clean": True, "digest": "",
                  "items": []}
        with mock.patch.object(registry.git, "probe_dirty_digest",
                               return_value=digest):
            report = registry.reconcile(self.conn, evidence)
        finding = [f for f in report["findings"] if f.get("claim_id")][0]
        self.assertEqual(finding["kind"], registry.UNKNOWN)
        bad = dict(evidence)
        bad["extra"] = "x"
        self.assertFalse(registry.reconcile(self.conn, bad)["ok"])

    def test_falsy_list_subclass_errors_not_treated_as_empty(self) -> None:
        class FalsyList(list):
            def __bool__(self):
                return False

        secret = "SYNTH_SECRET"
        bad = dict(self.evidence())
        bad["errors"] = FalsyList([secret])
        report = registry.reconcile(self.conn, bad)
        self.assertFalse(report["ok"])
        self.assertNotIn(secret, repr(report))

    def test_probe_and_registry_agree_on_worktree_state(self) -> None:
        sha = "a" * 40
        attached_text = ("worktree %s\0HEAD %s\0branch refs/heads/main\0\0"
                         % (self.repo, sha))
        with mock.patch.object(git, "_run_git", return_value=(0, attached_text)):
            probe = git.probe_worktrees(str(self.repo))
        self.assertTrue(probe["ok"])
        attached = self._worktree_item()
        attached["path"] = str(self.repo)
        with mock.patch.object(registry.git, "probe_worktrees",
                               return_value=self._probe([attached])), \
                mock.patch.object(registry.git, "probe_baseline_ref",
                                  return_value=self._probe([])):
            self.assertTrue(
                registry.gather_evidence(str(self.repo), EMPTY)["ok"])
        # A bare record with neither branch nor detached: BOTH reject it.
        bare_text = "worktree %s\0HEAD %s\0bare\0\0" % (self.repo, sha)
        with mock.patch.object(git, "_run_git", return_value=(0, bare_text)):
            probe = git.probe_worktrees(str(self.repo))
        self.assertFalse(probe["ok"])
        bare = self._worktree_item()
        bare.update(path=str(self.repo), branch="", detached=False, bare=True)
        with mock.patch.object(registry.git, "probe_worktrees",
                               return_value=self._probe([bare])), \
                mock.patch.object(registry.git, "probe_baseline_ref",
                                  return_value=self._probe([])):
            self.assertFalse(
                registry.gather_evidence(str(self.repo), EMPTY)["ok"])

    def test_status_flags_have_one_derivation(self) -> None:
        records = git.parse_status_porcelain(" M x\0")
        self.assertEqual(
            (records[0]["staged"], records[0]["unstaged"],
             records[0]["untracked"]), git.status_flags(" ", "M"))
        records = git.parse_status_porcelain("?? y\0")
        self.assertEqual(
            (records[0]["staged"], records[0]["unstaged"],
             records[0]["untracked"]), git.status_flags("?", "?"))

    def test_record_observation_hostile_serialization_fails_closed(self) -> None:
        secret = "SYNTH_SECRET"

        class BoomValue:
            def __repr__(self):
                raise RuntimeError(secret)

        class BoomDict(dict):
            def items(self):
                raise RuntimeError(secret)

            def keys(self):
                raise RuntimeError(secret)

        for payload in (BoomDict(), {"k": BoomValue()}):
            with self.subTest(payload=type(payload).__name__):
                try:
                    registry.record_observation(self.conn, secret, secret,
                                                secret, payload)
                except registry.RegistryError as exc:
                    frames = self._library_frames(exc)
                    blob = " ".join(repr(sorted(f.items())) for f in frames)
                    self.assertNotIn(secret, blob)
                    self.assertFalse(
                        any(value is self.conn
                            for f in frames for value in f.values()))
                else:
                    self.fail("expected RegistryError")


    def test_write_gate_refuses_repo_not_matching_identity(self) -> None:
        self.propose("a", ["src"])
        spec = ClaimSpec(run_id="run", work_key="a", owner="alice",
                         mode="WRITE", worktree=str(self.repo),
                         branch="main")
        evidence = self.evidence()
        # ``/etc`` is a real existing directory but NOT the claimed repository.
        bad = dict(evidence)
        bad["repo"] = "/etc"
        self.assertFalse(registry.reconcile(self.conn, bad)["ok"])
        refusal = registry.write_gate(self.conn, spec, bad)
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal["status"], registry.NEEDS_RECONCILIATION)
        self.assertEqual(registry.list_active_claims(self.conn), [])

    def test_observation_subclasses_rejected_and_copied(self) -> None:
        secret = "SYNTH_SECRET"

        class SneakyStr(str):
            pass

        class SneakyAgent(AgentObservation):
            pass

        bad = registry.gather_evidence(
            str(self.repo),
            RuntimeObservation(True, (AgentObservation(
                SneakyStr(secret), str(self.repo), "working"),)))
        self.assertFalse(bad["ok"])
        self.assertNotIn(secret, repr(bad))
        subclass = registry.gather_evidence(
            str(self.repo),
            RuntimeObservation(True, (SneakyAgent("a1", str(self.repo),
                                                  "working"),)))
        self.assertFalse(subclass["ok"])
        good = AgentObservation("a1", str(self.repo), "working")
        evidence = registry.gather_evidence(
            str(self.repo), RuntimeObservation(True, (good,)))
        self.assertTrue(evidence["ok"])
        canonical_agent = evidence["observation"].agents[0]
        self.assertIs(type(canonical_agent), AgentObservation)
        self.assertIsNot(canonical_agent, good)

    def test_dirty_digest_consumer_reads_canonical_copy(self) -> None:
        secret = "SYNTH_SECRET"
        probe = {"ok": True, "source": "git", "reason": "", "error": "",
                 "worktree": str(self.repo), "clean": False,
                 "digest": "a" * 16}
        canonical = registry._canonical_probe(probe, "dirty_digest")
        self.assertIsNotNone(canonical)
        self.assertIs(type(canonical), dict)
        self.assertIsNot(canonical, probe)
        probe["digest"] = secret
        self.assertEqual(canonical["digest"], "a" * 16)
        # reconcile reads only the canonical copy.
        self.propose("a", ["src"])
        self.claim("a", "alice", worktree=str(self.repo), branch="main")
        evidence = self.evidence()
        with mock.patch.object(registry.git, "probe_dirty_digest",
                               return_value=dict(probe)):
            report = registry.reconcile(self.conn, evidence)
        self.assertNotIn(secret, repr(report))


if __name__ == "__main__":
    unittest.main()
