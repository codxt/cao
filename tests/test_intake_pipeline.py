"""Synthetic tests for the B8 bounded Intake pipeline and its daemon wiring.

Covers: bounded deterministic discovery; triage and enqueue of an eligible READ
submission against the *trusted* envelope scope; the hold with an actionable
reason when no standing authority is present; the skip of a submission the
envelope does not cover; the hold of a non-READ submission; restart idempotency;
the per-tick bound; and the end-to-end proof that the native daemon creates
exactly one READ team and one Work Registry claim with durable B1->B3->B6->B7
evidence, and that a repeated tick duplicates nothing.

Nothing here is shipped and no real payload is used. The protected grant lives
in a temp directory; the runtime is a fake materialized in the same temp tree.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest import mock

from cao import (intake, intake_authority, intake_handoff, intake_pipeline,
                 intake_queue, intake_triage, product_config, product_daemon,
                 product_driver, product_state, registry, team)

_HAVE_GIT = shutil.which("git") is not None

_READ_REQUEST = (
    "Read-only audit of the scoped source. Revalidate the default branch and "
    "report evidence. Never merge, deploy, publish or change credentials."
)


def run_git(cwd, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc.stdout.decode("utf-8", "replace")


def make_repo(root: Path, name: str = "repo") -> Path:
    path = root / name
    path.mkdir()
    run_git(path, "init", "-b", "main")
    run_git(path, "config", "user.email", "test@example.invalid")
    run_git(path, "config", "user.name", "Test")
    run_git(path, "config", "commit.gpgsign", "false")
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    run_git(path, "add", "-A")
    run_git(path, "commit", "-m", "init")
    return path


def grant_document(repo: str, **overrides):
    # A valid grant carries an effective bounded validity window that is live
    # whenever the test runs: one day ago to 29 days ahead (within TTL bound).
    now = datetime.now(timezone.utc)
    document = {
        "schema": intake_authority.AUTHORITY_SCHEMA,
        "operator": "operator-one",
        "owner": "operator-one",
        "role": "reader",
        "model": "opencode-go/deepseek-v4.1-flash",
        "privacy_qualified": True,
        "scope": {"repo": repo, "paths": ["a.txt"]},
        "source": {"ref": "main"},
        "submissions": None,
        "max_dispatches": 3,
        "granted_at": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (now + timedelta(days=29)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "revoked": False,
    }
    document.update(overrides)
    return document


class PipelineCase(unittest.TestCase):
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
        self.repo = make_repo(self.root)
        self.repo_real = os.path.realpath(str(self.repo))
        self.protected = self.root / "protected"
        self.protected.mkdir(mode=0o755)
        os.chmod(self.protected, 0o755)

    # -- fixtures ------------------------------------------------------------
    def submit(self, item_id: str, body: str = _READ_REQUEST, clock=None) -> str:
        return intake.submit(
            {"schema": intake.REQUEST_SCHEMA, "item_id": item_id, "body": body,
             "title": "synthetic", "source": "operator"},
            env=self.env, clock=clock,
        )["submission"]["submission_id"]

    @staticmethod
    def admitted(offset: int):
        """A pinned admission instant for ``intake.submit``.

        ``admitted_at`` is second-precision, so submission order alone never
        proves chronological order; a test states the durable admission times
        it expects discovery to honor.
        """
        moment = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(
            seconds=offset)
        return lambda: moment

    def write_grant(self, **overrides) -> str:
        document = grant_document(self.repo_real, **overrides)
        path = self.protected / intake_authority.DEFAULT_GRANT_BASENAME
        path.write_text(json.dumps(document), encoding="utf-8")
        os.chmod(path, 0o600)
        return str(self.protected)

    def standing(self, **overrides) -> dict:
        directory = self.write_grant(**overrides)
        return intake_authority.standing_authority(
            env=self.env, uid=os.getuid(), directory=directory,
            trusted_base=str(self.root))

    def tick(self, authority, **overrides):
        kwargs = {"env": self.env, "authority": authority}
        kwargs.update(overrides)
        return intake_pipeline.tick(**kwargs)


class DiscoveryTests(PipelineCase):
    def test_discovers_only_returns_bounded_admitted_ids(self) -> None:
        # Discovery follows durable admission order (admitted_at, then id).
        # These two sha-derived ids sort opposite to their admission, so a
        # lexicographic expectation would return them reversed.
        first = self.submit("pipe-disc-a", clock=self.admitted(0))
        second = self.submit("pipe-disc-b", clock=self.admitted(1))
        self.assertGreater(first, second)  # sha order != admission order
        ordered = [first, second]
        self.assertEqual(intake_pipeline.discover(self.env), ordered)
        self.assertEqual(intake_pipeline.discover(self.env, limit=1), ordered[:1])

    def test_no_valid_authority_holds_and_persists_nothing(self) -> None:
        self.submit("pipe-noauth")
        report = self.tick(None)
        self.assertEqual(report["status"], "HOLD")
        self.assertEqual(report["code"], "authority_unavailable")
        self.assertEqual(report["discovered"], 0)
        self.assertFalse(intake_queue.list_intents(self.env))
        self.assertFalse(intake_triage.list_packets(self.env))


class EnqueueTests(PipelineCase):
    def test_eligible_read_is_triaged_and_queued_with_the_trusted_scope(self) -> None:
        sid = self.submit("pipe-ok")
        report = self.tick(self.standing())
        self.assertEqual(report["status"], "RUNNING")
        self.assertEqual(report["discovered"], 1)
        self.assertEqual(report["enqueued"], 1)
        intents = intake_queue.list_intents(self.env)
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]["binding"]["submission_id"], sid)
        self.assertEqual(intents[0]["binding"]["source"]["repo"], self.repo_real)
        self.assertEqual(intents[0]["binding"]["source"]["ref"], "main")
        self.assertEqual(len(intake_triage.list_packets(self.env)), 1)
        # The pipeline never dispatches work itself.
        self.assertFalse(team.list_teams(env=self.env))

    def test_repo_comes_from_the_envelope_not_the_submission(self) -> None:
        body = ("Read-only audit of /tmp/opencode/evil-other-checkout; never "
                "merge or deploy.")
        sid = self.submit("pipe-evil", body)
        report = self.tick(self.standing())
        self.assertEqual(report["enqueued"], 1)
        intent = intake_queue.list_intents(self.env)[0]
        self.assertEqual(intent["binding"]["submission_id"], sid)
        self.assertEqual(intent["binding"]["source"]["repo"], self.repo_real)

    def test_submission_outside_the_envelope_is_not_processed(self) -> None:
        self.submit("pipe-outside")
        report = self.tick(self.standing(submissions=["f" * 64]))
        self.assertEqual(report["discovered"], 0)
        self.assertFalse(intake_queue.list_intents(self.env))

    def test_non_read_submission_is_held_and_persists_no_intent(self) -> None:
        self.submit("pipe-write",
                    "Apply the correction and push it to the remote branch.")
        report = self.tick(self.standing())
        self.assertEqual(report["enqueued"], 0)
        self.assertEqual(report["submissions"][0]["queue_status"], "HOLD")
        self.assertFalse(intake_queue.list_intents(self.env))

    def test_mixed_effect_read_request_is_held(self) -> None:
        self.submit("pipe-mixed",
                    "Read-only audit of the source, then one squash merge of PR 7.")
        report = self.tick(self.standing())
        self.assertEqual(report["enqueued"], 0)
        self.assertFalse(intake_queue.list_intents(self.env))

    def test_repeated_ticks_are_idempotent(self) -> None:
        self.submit("pipe-repeat")
        authority = self.standing()
        first = self.tick(authority)
        second = self.tick(authority)
        self.assertEqual(first["enqueued"], 1)
        self.assertEqual(second["enqueued"], 1)  # DUPLICATE still counts as queued
        self.assertEqual(second["submissions"][0]["queue_status"], "DUPLICATE")
        self.assertEqual(len(intake_queue.list_intents(self.env)), 1)
        self.assertEqual(len(intake_triage.list_packets(self.env)), 1)

    def test_tick_is_bounded(self) -> None:
        self.submit("pipe-bound-a")
        self.submit("pipe-bound-b")
        report = self.tick(self.standing(), limit=1)
        self.assertEqual(report["discovered"], 1)
        self.assertEqual(len(intake_queue.list_intents(self.env)), 1)

    def test_dry_run_creates_no_intent(self) -> None:
        self.submit("pipe-dry")
        report = self.tick(self.standing(), dry_run=True)
        self.assertEqual(report["status"], "DRY_RUN")
        self.assertFalse(intake_queue.list_intents(self.env))
        # A dry run still records the deterministic advisory packet.
        self.assertEqual(len(intake_triage.list_packets(self.env)), 1)


class PipelineBoundaryTests(PipelineCase):
    def test_pipeline_creates_no_team_no_claim_and_no_ledger(self) -> None:
        self.submit("pipe-safety")
        self.tick(self.standing())
        self.assertFalse(team.list_teams(env=self.env))
        self.assertFalse(
            (Path(self.env["XDG_STATE_HOME"]) / "cao-work-registry").exists())


class VerdictGateTests(PipelineCase):
    """Only an explicitly verified standing record drives discovery/enqueue."""

    def test_refused_verdicts_discover_and_enqueue_nothing(self) -> None:
        self.submit("pipe-gate")
        cases = {
            "revoked": self.standing(revoked=True),
            "expired": self.standing(
                granted_at="2020-01-01T00:00:00Z",
                expires_at="2020-01-02T00:00:00Z"),
            "future": self.standing(
                granted_at="2099-06-01T00:00:00Z",
                expires_at="2099-06-30T00:00:00Z"),
        }
        for name, record in cases.items():
            with self.subTest(name=name):
                self.assertEqual(record["verdict"], "REFUSED")
                report = self.tick(record)
                self.assertEqual(report["status"], "HOLD")
                self.assertEqual(report["discovered"], 0)
                self.assertEqual(report["enqueued"], 0)
                self.assertFalse(intake_queue.list_intents(self.env))
                self.assertFalse(intake_triage.list_packets(self.env))
        self.assertFalse(team.list_teams(env=self.env))

    def test_unverified_records_never_pass_the_gate(self) -> None:
        self.submit("pipe-forged")
        # A fake authenticated bit, a bare envelope and a demand for a
        # missing-allowlist model are all non-SUPPORTED records here.
        sideways = intake_handoff.handoff_plan(
            self.submit("pipe-forged-plan"), repo=self.repo_real,
            source_ref="main", env=self.env, dry_run=True)["plan"]
        cases = {
            "fake-authenticated": {"authenticated": True, "reason": "forged"},
            "bare-envelope": {"envelope": sideways["binding"]},
            "unknown-verdict": {"verdict": "UNKNOWN", "code": "authority_error",
                                "reason": "unreadable", "envelope": None,
                                "adapter": None},
        }
        for name, record in cases.items():
            with self.subTest(name=name):
                report = self.tick(record)
                self.assertEqual(report["status"], "HOLD")
                self.assertEqual(report["discovered"], 0)
                self.assertFalse(intake_queue.list_intents(self.env))

    def test_zero_limit_processes_nothing(self) -> None:
        self.submit("pipe-zero")
        self.assertEqual(intake_pipeline.discover(self.env, limit=0), [])
        report = self.tick(self.standing(), limit=0)
        self.assertEqual(report["discovered"], 0)
        self.assertEqual(report["processed"], 0)
        self.assertFalse(intake_queue.list_intents(self.env))

    def test_discovery_honors_the_scope_before_the_bound(self) -> None:
        # Admission order, not lexicographic sha order, decides which of the
        # two submissions is the earliest one the bound may return first.
        earliest = self.submit("disc-a", clock=self.admitted(0))
        latest = self.submit("disc-b", clock=self.admitted(1))
        self.assertGreater(earliest, latest)  # sha order != admission order
        allowed = {latest}
        self.assertEqual(
            intake_pipeline.discover(self.env, limit=1, allowed=allowed),
            [latest])
        # The unfiltered bound still returns the earliest admission first.
        self.assertEqual(
            intake_pipeline.discover(self.env, limit=1),
            [earliest])


class FairnessTests(PipelineCase):
    """Grant-scope filtering precedes the bound; a rotating cursor is fair."""

    _HELD_BODY = "Apply the correction and push it to the remote branch."

    def test_thirty_three_held_candidates_never_starve_the_authorized_one(self) -> None:
        for index in range(32):
            self.submit(f"pipe-held-{index:02d}", self._HELD_BODY)
        eligible = self.submit("pipe-fair-33")
        self.assertEqual(len(intake_pipeline.discover(self.env, limit=64)), 33)
        ticks = 0
        for cursor in range(33):
            ticks = cursor + 1
            self.tick(self.standing(), cursor=cursor)
            if len(intake_queue.list_intents(self.env)) == 1:
                break
        # The authorized submission is processed within two rotating windows,
        # however the 32 held earlier candidates are ordered.
        self.assertLessEqual(ticks, 2)
        intents = intake_queue.list_intents(self.env)
        self.assertEqual(
            [intent["binding"]["submission_id"] for intent in intents],
            [eligible])

    def test_every_authorized_submission_is_eventually_processed(self) -> None:
        sids = [self.submit(f"pipe-card-{index:02d}") for index in range(33)]
        author = self.tick(self.standing(), cursor=0)
        self.assertEqual(author["discovered"], 32)
        # The remaining authorized submission is never silently dropped: the
        # next tick's rotated window covers it.
        follow = self.tick(self.standing(), cursor=1)
        self.assertEqual(len(intake_queue.list_intents(self.env)), 33)
        self.assertEqual(follow["discovered"], 32)

    def test_independent_requests_continue_beside_held_ones(self) -> None:
        for index in range(32):
            self.submit(f"pipe-held-{index:02d}", self._HELD_BODY)
        first_new = self.submit("pipe-after-1")
        self.tick(self.standing(), cursor=0)
        follow = self.tick(self.standing(), cursor=1)
        self.assertEqual(follow["enqueued"], 1)
        self.assertEqual(
            [i["binding"]["submission_id"] for i in intake_queue.list_intents(self.env)],
            [first_new])
        # A second independent authorized request is processed on a later tick.
        second_new = self.submit("pipe-after-2")
        later = self.tick(self.standing(), cursor=2)
        self.assertLessEqual(later["discovered"], intake_pipeline.DISCOVERY_LIMIT)
        self.assertEqual(
            {i["binding"]["submission_id"] for i in intake_queue.list_intents(self.env)},
            {first_new, second_new})

    def test_cursor_changes_only_the_window_not_the_results(self) -> None:
        self.submit("pipe-cursor-a")
        self.submit("pipe-cursor-b")
        report_a = self.tick(self.standing(), cursor=0)
        report_b = self.tick(self.standing(), cursor=0)
        # Same cursor: identical processing order (replay determinism).
        self.assertEqual(
            [s["submission_id"] for s in report_a["submissions"]],
            [s["submission_id"] for s in report_b["submissions"]])


try:  # the deterministic fake runtime is a shared hermetic fixture
    from test_product_e2e_fake import FAKE_RUNTIME, FakeClock
except ImportError:  # pragma: no cover - package-style invocation
    from tests.test_product_e2e_fake import FAKE_RUNTIME, FakeClock


class StandingReadEndToEndTests(unittest.TestCase):
    """The native daemon turns a standing-approved READ request into one team."""

    def setUp(self) -> None:
        if not _HAVE_GIT:
            self.skipTest("git is required")
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.repo = make_repo(self.root, "project")
        self.repo_real = os.path.realpath(str(self.repo))
        self.state_home = self.root / "state"
        self.config_home = self.root / "config"
        self.config_path = self.config_home / "cao" / "config.json"
        self.db = str(self.root / "registry.sqlite3")
        self.state_file = self.root / "fake-state.json"
        self.log_file = self.root / "fake-invocations.txt"
        self.fake_exe = self.root / "fake-runtime"
        self.fake_exe.write_text(FAKE_RUNTIME, encoding="utf-8")
        self.fake_exe.chmod(0o755)
        self.env = {
            "XDG_STATE_HOME": str(self.state_home),
            "XDG_CONFIG_HOME": str(self.config_home),
            # Consumption derives quota evidence from the governed ledger, so
            # the e2e fixture provisions the same empty registry up front.
            "CAO_REGISTRY_DB": self.db,
        }
        conn = registry.connect(self.db)
        conn.close()
        product_config.write_config(
            {
                "schema": product_config.SCHEMA,
                "runtime_id": "runtime",
                "registry_db": self.db,
                "provider": {"cli": "opencode", "id": "opencode", "model": "m"},
                "team_model_allowlist": ["opencode/m"],
                "task": {"agent_kind": "opencode"},
                "daemon": {"intake_pipeline": True, "intake_queue": True},
            },
            path=str(self.config_path),
        )
        product_state.write_managed(
            {"schema": product_state.MANAGED_SCHEMA, "path": str(self.fake_exe)},
            self.env,
        )
        self.protected = self.root / "protected"
        self.protected.mkdir(mode=0o755)
        os.chmod(self.protected, 0o755)
        grant = grant_document(
            self.repo_real,
            # The fake clock is pinned to 2026-01-01, so the grant's effective
            # window around it must be written explicitly (within TTL bound).
            model="opencode/m",
            granted_at="2025-12-31T00:00:00Z",
            expires_at="2026-01-30T00:00:00Z",
        )
        grant_path = self.protected / intake_authority.DEFAULT_GRANT_BASENAME
        grant_path.write_text(json.dumps(grant), encoding="utf-8")
        os.chmod(grant_path, 0o600)

        patcher = mock.patch.dict(
            os.environ,
            {**self.env, "FAKE_RUNTIME_STATE": str(self.state_file),
             "FAKE_RUNTIME_LOG": str(self.log_file)},
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _reader(self, env, clock):
        return intake_authority.standing_authority(
            env=env, clock=clock, uid=os.getuid(),
            directory=str(self.protected), trusted_base=str(self.root))

    def _daemon(self) -> product_daemon.Daemon:
        return product_daemon.Daemon(
            driver=product_driver.RuntimeDriver(executable=str(self.fake_exe)),
            config=product_config.load_config(str(self.config_path)),
            env=self.env,
            registry_db=self.db,
            clock=FakeClock(),
            sleep=lambda _seconds: None,
            spawn=lambda: 0,
            alive=lambda _pid: False,
            terminate=lambda _pid: None,
            authority_reader=self._reader,
        )

    def _submission(self) -> str:
        return intake.submit(
            {"schema": intake.REQUEST_SCHEMA, "item_id": "b8-e2e",
             "body": _READ_REQUEST, "title": "synthetic", "source": "operator"},
            env=self.env,
        )["submission"]["submission_id"]

    def test_one_team_and_one_claim_with_durable_evidence(self) -> None:
        sid = self._submission()
        product_state.write_daemon(
            {**product_state.default_daemon_record(),
             "desired": "running", "observed": "running"}, self.env)

        first = self._daemon().tick()
        self.assertEqual(first["status"], "RUNNING")
        teams = team.list_teams(env=self.env)
        self.assertEqual([view["team_id"] for view in teams],
                         ["intake-read-" + sid[:16]])
        team_id = teams[0]["team_id"]
        self.assertEqual(team._team_claim_count(self.env, team_id), 1)

        # Durable B1 -> B3 -> B6 -> B7 evidence exists for the submission.
        self.assertTrue(intake.read_submission(sid, self.env))
        self.assertEqual(len(intake_triage.list_packets(self.env)), 1)
        self.assertIsNotNone(intake_handoff.recover_plan(sid, env=self.env))
        intent = intake_queue.list_intents(self.env)[0]
        self.assertEqual(intent["status"], "STARTED")
        self.assertIn(intent["receipt"]["decision"], ("ADVANCE", "NONE"))
        self.assertEqual(intent["binding"]["submission_id"], sid)

        # A repeated tick duplicates neither the team nor the claim.
        second = self._daemon().tick()
        self.assertEqual(second["status"], "RUNNING")
        self.assertEqual(len(team.list_teams(env=self.env)), 1)
        self.assertEqual(team._team_claim_count(self.env, team_id), 1)

    def test_absent_protected_grant_holds_and_creates_no_team(self) -> None:
        sid = self._submission()
        product_state.write_daemon(
            {**product_state.default_daemon_record(),
             "desired": "running", "observed": "running"}, self.env)
        os.remove(self.protected / intake_authority.DEFAULT_GRANT_BASENAME)
        report = self._daemon().tick()
        authority = [e for e in report["effects"]
                     if e.get("action") == "intake_authority"]
        self.assertTrue(authority)
        self.assertEqual(authority[0]["status"], "UNAVAILABLE")
        self.assertFalse(team.list_teams(env=self.env))
        # The intent is queued but the first dispatch is held, never fabricated.
        intents = intake_queue.list_intents(self.env)
        if intents:
            self.assertEqual(intents[0]["status"], "HELD")
        self.assertEqual(team._team_claim_count(self.env, "intake-read-" + sid[:16]), 0)


if __name__ == "__main__":
    unittest.main()
