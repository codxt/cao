"""Synthetic security tests for the B8 standing intake authority.

Covers: the descriptor-relative, owner/mode-checked protected-grant reader (a
host-shaped accept path and the refusal of a symlink, a foreign owner, a
group/other-writable directory or file, an oversized file, malformed JSON,
duplicate keys and non-finite numbers, and a genuinely absent grant); the strict
grant validation; the verified in-process adapter that binds the exact derived
B6 plan digest and refuses an expired, revoked, out-of-scope, wrong-source,
wrong-model or quota-exhausted envelope; consumption derived from the existing
work artifacts; the read-only status projection; and the absence of any new
authority ledger or write.

Nothing here is shipped and no real payload is used. Every value is synthetic
and the protected grant lives in a temp directory, never in a real system path.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest import mock

from cao import (intake, intake_authority, intake_handoff, intake_queue,
                 intake_triage, product_config, registry, team)

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


def grant_document(**overrides):
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
        "scope": {"repo": "/tmp/opencode/checkout", "paths": ["a.txt"]},
        "source": {"ref": "main"},
        "submissions": None,
        "max_dispatches": 3,
        "granted_at": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (now + timedelta(days=29)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "revoked": False,
    }
    document.update(overrides)
    return document


class ProtectedGrantCase(unittest.TestCase):
    """A host-shaped protected directory the reader can validate hermetically."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        # The reader validates components *below* trusted_base and checks the
        # leaf for the trusted owner. A temp root cannot be chowned to root, so
        # the host-shaped accept path uses this user's uid; the production
        # default (uid 0) is exercised as a refusal in its own test.
        self.trusted = self.root
        self.uid = os.getuid()
        self.protected = self.root / "protected"
        self.protected.mkdir(mode=0o755)
        os.chmod(self.protected, 0o755)
        self.grant_path = self.protected / "intake-authority.json"

    def write_grant(self, document: dict, mode: int = 0o600) -> None:
        self.grant_path.write_text(json.dumps(document), encoding="utf-8")
        os.chmod(self.grant_path, mode)

    def read(self, **overrides):
        kwargs = {
            "directory": str(self.protected),
            "uid": self.uid,
            "trusted_base": str(self.trusted),
        }
        kwargs.update(overrides)
        return intake_authority.read_protected_grant(**kwargs)


class ProtectedGrantReaderTests(ProtectedGrantCase):
    def test_accepts_a_host_shaped_grant(self) -> None:
        self.write_grant(grant_document())
        raw = self.read()
        self.assertEqual(raw["schema"], intake_authority.AUTHORITY_SCHEMA)
        envelope = intake_authority.validate_grant(raw)
        self.assertEqual(envelope["operator"], "operator-one")
        self.assertTrue(envelope["privacy_qualified"])

    def test_absent_grant_is_unavailable_not_an_error(self) -> None:
        with self.assertRaises(intake_authority.AuthorityUnavailable):
            self.read()

    def test_absent_directory_is_unavailable(self) -> None:
        with self.assertRaises(intake_authority.AuthorityUnavailable):
            self.read(directory=str(self.root / "missing"))

    def test_wrong_owner_is_refused(self) -> None:
        self.write_grant(grant_document())
        # Production requires uid 0; a same-user grant is never proof.
        with self.assertRaises(intake_authority.AuthorityStateError):
            self.read(uid=0)

    def test_group_or_other_writable_file_is_refused(self) -> None:
        self.write_grant(grant_document(), mode=0o666)
        with self.assertRaises(intake_authority.AuthorityStateError):
            self.read()

    def test_group_or_other_writable_directory_is_refused(self) -> None:
        self.write_grant(grant_document())
        os.chmod(self.protected, 0o777)
        with self.assertRaises(intake_authority.AuthorityStateError):
            self.read()

    def test_symlinked_component_is_refused(self) -> None:
        self.write_grant(grant_document())
        link = self.root / "link"
        os.symlink(str(self.protected), str(link))
        with self.assertRaises(intake_authority.AuthorityStateError):
            self.read(directory=str(link))

    def test_symlinked_grant_file_is_refused(self) -> None:
        target = self.root / "elsewhere.json"
        target.write_text(json.dumps(grant_document()), encoding="utf-8")
        os.chmod(target, 0o600)
        os.symlink(str(target), str(self.grant_path))
        with self.assertRaises(intake_authority.AuthorityStateError):
            self.read()

    def test_non_regular_grant_is_refused(self) -> None:
        self.grant_path.mkdir()
        with self.assertRaises(intake_authority.AuthorityStateError):
            self.read()

    def test_oversized_grant_is_refused(self) -> None:
        self.grant_path.write_bytes(b"{" + b" " * (intake_authority.MAX_GRANT_BYTES + 1))
        os.chmod(self.grant_path, 0o600)
        with self.assertRaises(intake_authority.AuthorityStateError):
            self.read()

    def test_malformed_and_duplicate_and_nonfinite_are_refused(self) -> None:
        self.grant_path.write_text("{not json", encoding="utf-8")
        os.chmod(self.grant_path, 0o600)
        with self.assertRaises(intake_authority.AuthorityInvalid):
            self.read()
        self.grant_path.write_text('{"a": 1, "a": 2}', encoding="utf-8")
        os.chmod(self.grant_path, 0o600)
        with self.assertRaises(intake_authority.AuthorityInvalid):
            self.read()
        self.grant_path.write_text('{"a": Infinity}', encoding="utf-8")
        os.chmod(self.grant_path, 0o600)
        with self.assertRaises(intake_authority.AuthorityInvalid):
            self.read()

    def test_a_swap_after_validation_cannot_redirect_the_read(self) -> None:
        # The grant is read through the pinned descriptor, so replacing the
        # path with a symlink after the directory is validated cannot substitute
        # content: the open happens descriptor-relative and no-follow.
        self.write_grant(grant_document())
        raw = self.read()
        self.assertEqual(raw["operator"], "operator-one")
        replacement = self.root / "forged.json"
        replacement.write_text(json.dumps(grant_document(operator="attacker")),
                               encoding="utf-8")
        os.chmod(replacement, 0o600)
        os.remove(self.grant_path)
        os.symlink(str(replacement), str(self.grant_path))
        with self.assertRaises(intake_authority.AuthorityStateError):
            self.read()


class GrantValidationTests(unittest.TestCase):
    def test_valid_document_round_trips(self) -> None:
        envelope = intake_authority.validate_grant(grant_document())
        self.assertEqual(envelope["scope"]["paths"], ["a.txt"])
        self.assertIsNone(envelope["submissions"])

    def test_defects_are_refused(self) -> None:
        cases = {
            "schema": grant_document(schema="other"),
            "unknown-key": grant_document(extra=1),
            "missing-key": {k: v for k, v in grant_document().items()
                            if k != "revoked"},
            "privacy": grant_document(privacy_qualified=False),
            "model": grant_document(model="not-a-model"),
            "repo": grant_document(scope={"repo": "relative", "paths": []}),
            "paths": grant_document(scope={"repo": "/tmp/opencode/x",
                                            "paths": ["../escape"]}),
            "ref": grant_document(source={"ref": "bad ref!"}),
            "expires": grant_document(granted_at="2026-02-01T00:00:00Z",
                                      expires_at="2026-01-01T00:00:00Z"),
            "quota": grant_document(max_dispatches=0),
            "revoked": grant_document(revoked="no"),
            "submissions": grant_document(submissions=["short"]),
        }
        for name, document in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(intake_authority.AuthorityInvalid):
                    intake_authority.validate_grant(document)


class AuthorityDispatchCase(unittest.TestCase):
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
        # Consumption derives quota evidence from the existing Work Registry,
        # so the fixture provisions a real, empty governed ledger.
        ledger = self.root / "registry.sqlite3"
        conn = registry.connect(str(ledger))
        conn.close()
        self.env["CAO_REGISTRY_DB"] = str(ledger)
        product_config.write_config(
            {"schema": product_config.SCHEMA,
             "provider": {"cli": "opencode", "model": "opencode-go/deepseek-v4.1-flash"},
             "team_model_allowlist": ["opencode-go/deepseek-v4.1-flash"]},
            product_config.config_path(self.env))

    def admitted(self, item_id: str, body: str = _READ_REQUEST) -> str:
        sid = intake.submit(
            {"schema": intake.REQUEST_SCHEMA, "item_id": item_id, "body": body,
             "title": "synthetic", "source": "operator"}, env=self.env,
        )["submission"]["submission_id"]
        intake_triage.triage(sid, env=self.env)
        return sid

    def envelope(self, **overrides) -> dict:
        document = grant_document(
            scope={"repo": self.repo_real, "paths": ["a.txt"]},
        )
        document.update(overrides)
        return intake_authority.validate_grant(document)

    def plan(self, sid: str) -> dict:
        return intake_handoff.handoff_plan(
            sid, repo=self.repo_real, source_ref="main", env=self.env,
            dry_run=True)["plan"]

    def adapter(self, envelope=None):
        return intake_authority.OperatorAuthority(
            envelope or self.envelope(), env=self.env)


class AdapterTests(AuthorityDispatchCase):
    def test_adapter_binds_the_exact_plan_digest(self) -> None:
        sid = self.admitted("auth-bind")
        plan = self.plan(sid)
        result = self.adapter()(submission_id=sid, plan=plan, env=self.env)
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["operator"], "operator-one")
        self.assertEqual(result["grant"]["plan_digest"],
                         intake_handoff._binding_digest(plan))
        self.assertTrue(result["grant"]["read_only"])
        self.assertTrue(result["grant"]["privacy_qualified"])
        self.assertEqual(
            set(result["grant"]),
            {"plan_digest", "read_only", "owner", "scope", "role", "model",
             "privacy_qualified", "granted_at"},
        )

    def test_adapter_dispatches_exactly_one_read_team_through_b6(self) -> None:
        sid = self.admitted("auth-dispatch")
        dispatched = intake_handoff.handoff_plan(
            sid, repo=self.repo_real, source_ref="main", env=self.env,
            operator_authority=self.adapter())
        self.assertEqual(dispatched["status"], "ADMITTED")
        teams = team.list_teams(env=self.env)
        self.assertEqual([t["team_id"] for t in teams], ["intake-read-" + sid[:16]])
        self.assertEqual(
            team.status("intake-read-" + sid[:16], env=self.env)["desired"],
            "running")

    def test_adapter_refuses_expired_grant(self) -> None:
        sid = self.admitted("auth-expired")
        envelope = self.envelope(
            granted_at="2020-01-01T00:00:00Z", expires_at="2020-01-02T00:00:00Z")
        result = intake_authority.OperatorAuthority(
            envelope, env=self.env)(submission_id=sid, plan=self.plan(sid),
                                    env=self.env)
        self.assertFalse(result["authenticated"])

    def test_adapter_refuses_revoked_grant(self) -> None:
        sid = self.admitted("auth-revoked")
        result = self.adapter(self.envelope(revoked=True))(
            submission_id=sid, plan=self.plan(sid), env=self.env)
        self.assertFalse(result["authenticated"])

    def test_adapter_refuses_out_of_scope_submission(self) -> None:
        sid = self.admitted("auth-scope")
        envelope = self.envelope(submissions=["f" * 64])
        verdict = intake_authority.OperatorAuthority(
            envelope, env=self.env).evaluate(
            submission_id=sid, plan=self.plan(sid), env=self.env)
        self.assertEqual(verdict["verdict"], "REFUSED")
        self.assertEqual(verdict["code"], "authority_submission_out_of_scope")

    def test_adapter_refuses_wrong_source_repository(self) -> None:
        sid = self.admitted("auth-repo")
        other = make_repo(self.root, "other")
        envelope = self.envelope(
            scope={"repo": os.path.realpath(str(other)), "paths": ["a.txt"]})
        verdict = intake_authority.OperatorAuthority(
            envelope, env=self.env).evaluate(
            submission_id=sid, plan=self.plan(sid), env=self.env)
        self.assertEqual(verdict["code"], "authority_source_mismatch")

    def test_adapter_refuses_wrong_source_ref(self) -> None:
        sid = self.admitted("auth-ref")
        envelope = self.envelope(source={"ref": "other-branch"})
        verdict = intake_authority.OperatorAuthority(
            envelope, env=self.env).evaluate(
            submission_id=sid, plan=self.plan(sid), env=self.env)
        self.assertEqual(verdict["code"], "authority_source_ref_mismatch")

    def test_adapter_refuses_model_not_allowlisted(self) -> None:
        sid = self.admitted("auth-model")
        product_config.write_config(
            {"schema": product_config.SCHEMA,
             "provider": {"cli": "opencode", "model": "opencode-go/deepseek-v4.1-flash"},
             "team_model_allowlist": ["opencode-go/other-model"]},
            product_config.config_path(self.env))
        verdict = self.adapter().evaluate(
            submission_id=sid, plan=self.plan(sid), env=self.env)
        self.assertEqual(verdict["code"], "authority_model_not_allowlisted")

    def test_adapter_refuses_when_no_model_allowlist_is_configured(self) -> None:
        # Without a configured allowlist there is no verified policy, so new
        # automatic authority is refused (never granted by default).
        sid = self.admitted("auth-no-allowlist")
        product_config.write_config(
            {"schema": product_config.SCHEMA,
             "provider": {"cli": "opencode", "model": "opencode-go/deepseek-v4.1-flash"}},
            product_config.config_path(self.env))
        verdict = self.adapter().evaluate(
            submission_id=sid, plan=self.plan(sid), env=self.env)
        self.assertEqual(verdict["code"], "authority_model_policy_missing")

    def test_adapter_refuses_when_quota_is_exhausted(self) -> None:
        sid = self.admitted("auth-quota")
        envelope = self.envelope(max_dispatches=1)
        with mock.patch.object(intake_authority, "consumption",
                               return_value={"verdict": "SUPPORTED", "consumed": 1,
                                             "teams": [], "claims": [], "reason": None}):
            verdict = intake_authority.OperatorAuthority(
                envelope, env=self.env).evaluate(
                submission_id=sid, plan=self.plan(sid), env=self.env)
        self.assertEqual(verdict["code"], "authority_quota_exhausted")

    def test_adapter_fails_closed_when_quota_evidence_is_unavailable(self) -> None:
        sid = self.admitted("auth-quota-unknown")
        with mock.patch.object(intake_authority, "consumption",
                               return_value={"verdict": "UNKNOWN", "consumed": 0,
                                             "teams": [], "claims": [],
                                             "reason": "unreadable"}):
            verdict = self.adapter().evaluate(
                submission_id=sid, plan=self.plan(sid), env=self.env)
        self.assertEqual(verdict["code"], "authority_quota_evidence_incomplete")

    def test_a_refused_adapter_creates_no_team_or_claim(self) -> None:
        sid = self.admitted("auth-refused")
        result = intake_handoff.handoff_plan(
            sid, repo=self.repo_real, source_ref="main", env=self.env,
            operator_authority=self.adapter(self.envelope(revoked=True)))
        self.assertNotIn(result["status"], ("ADMITTED", "DISPATCH_PENDING"))
        self.assertFalse(team.list_teams(env=self.env))

    def test_revocation_between_enqueue_and_tick_holds_the_dispatch(self) -> None:
        # An intent queued before revocation is still governed at dispatch
        # time: the queue re-derives the authority verdict on every advance.
        sid = self.admitted("auth-revoke-after-enqueue")
        intake_queue.enqueue(sid, repo=self.repo_real, source_ref="main",
                             env=self.env)
        report = intake_queue.tick(
            env=self.env, operator_authority=self.adapter(
                self.envelope(revoked=True)))
        self.assertEqual(report["items"][0]["decision"], "HOLD")
        self.assertFalse(team.list_teams(env=self.env))


class ConsumptionTests(AuthorityDispatchCase):
    def test_consumption_counts_a_dispatched_bound_read_team(self) -> None:
        sid = self.admitted("auth-consumed")
        envelope = self.envelope()
        before = intake_authority.consumption(env=self.env, envelope=envelope)
        self.assertEqual(before["verdict"], "SUPPORTED")
        self.assertEqual(before["consumed"], 0)
        intake_queue.enqueue(sid, repo=self.repo_real, source_ref="main",
                             env=self.env)
        report = intake_queue.tick(
            env=self.env, operator_authority=self.adapter(envelope))
        self.assertEqual(report["items"][0]["decision"], "ADVANCE")
        after = intake_authority.consumption(env=self.env, envelope=envelope)
        self.assertEqual(after["consumed"], 1)
        self.assertIn("intake-read-" + sid[:16], after["teams"])

    def test_consumption_ignores_a_submission_outside_the_envelope(self) -> None:
        sid = self.admitted("auth-foreign")
        envelope = self.envelope(submissions=[sid])
        # A READ audit of a different submission is not counted for this scope.
        other = self.admitted("auth-foreign-other")
        manifest = intake_handoff._manifest(
            self.plan(other), {"role": "reader",
                               "model": "opencode-go/deepseek-v4.1-flash",
                               "repo": self.repo_real, "paths": ["a.txt"]})
        team.initialize(manifest, env=self.env)
        result = intake_authority.consumption(env=self.env, envelope=envelope)
        self.assertEqual(result["verdict"], "SUPPORTED")
        self.assertEqual(result["consumed"], 0)

    def test_consumption_is_unknown_when_team_state_is_unreadable(self) -> None:
        envelope = self.envelope()
        with mock.patch.object(intake_authority.team, "list_teams",
                               side_effect=Exception("boom")):
            result = intake_authority.consumption(env=self.env, envelope=envelope)
        self.assertEqual(result["verdict"], "UNKNOWN")


class ConsumptionCardinalityTests(unittest.TestCase):
    """Every team record is inspected; an overlarge store fails closed."""

    TOKEN = "b" * 64
    TEAM_ID = "intake-read-" + TOKEN[:16]

    def _views(self, eligible_at: int, corrupt: bool = False) -> list[dict]:
        mapped: list[dict] = [
            {"team_id": f"zzz{index:07d}", "status": "PAUSED"}
            for index in range(1024)
        ]
        eligible = {"team_id": self.TEAM_ID, "status": "PAUSED"}
        mapped.insert(eligible_at, eligible)
        if corrupt:
            mapped.append({"team_id": "z-later", "status": "CORRUPT"})
        return mapped

    def _envelope(self) -> dict:
        return {
            "operator": "op", "owner": "op", "role": "reader",
            "model": "opencode-go/deepseek-v4.1-flash",
            "privacy_qualified": True,
            "scope": {"repo": "/tmp/opencode/checkout", "paths": ["a.txt"]},
            "source": {"ref": "main"},
            "submissions": None,
            "max_dispatches": 3,
        }

    def _record(self) -> dict:
        return {
            "spec": {
                "tasks": [{
                    "mode": "read", "write_scope": [],
                    "prompt": "audit " + self.TOKEN,
                }],
            },
        }

    def _consume(self, listed: list[dict]) -> dict:
        with mock.patch.object(intake_authority.team, "list_teams",
                               return_value=listed), \
                mock.patch.object(intake_authority.team, "_required",
                                  return_value=self._record()):
            return intake_authority.consumption(
                env={}, envelope=self._envelope())

    def test_eligible_team_at_1025_is_counted_never_truncated(self) -> None:
        result = self._consume(self._views(eligible_at=1024))
        self.assertEqual(result["verdict"], "SUPPORTED")
        self.assertEqual(result["consumed"], 1)

    def test_corrupt_entry_beyond_1024_still_fails_closed(self) -> None:
        result = self._consume(self._views(eligible_at=0, corrupt=True))
        self.assertEqual(result["verdict"], "UNKNOWN")

    def test_excessive_cardinality_fails_closed(self) -> None:
        with mock.patch.object(intake_authority.team, "list_teams",
                               return_value=[None] * 5000):
            result = intake_authority.consumption(
                env={}, envelope=self._envelope())
        self.assertEqual(result["verdict"], "UNKNOWN")
        self.assertIn("inspection bound", result["reason"])


class RegistryEvidenceTests(AuthorityDispatchCase):
    """A missing, corrupt or symlinked Registry is UNKNOWN, never a new quota."""

    def _consume(self) -> dict:
        envelope = self.envelope()
        return intake_authority.consumption(env=self.env, envelope=envelope)

    def test_missing_registry_is_unknown(self) -> None:
        ledger = self.root / "state" / "cao-work-registry" / "registry.sqlite3"
        self.assertFalse(os.path.exists(str(ledger)))
        self.env["CAO_REGISTRY_DB"] = str(ledger)
        result = self._consume()
        self.assertEqual(result["verdict"], "UNKNOWN")
        self.assertIn("incomplete", result["reason"])

    def test_corrupt_registry_is_unknown(self) -> None:
        ledger = self.root / "registry.sqlite3"
        ledger.write_bytes(b"not a sqlite database at all")
        self.env["CAO_REGISTRY_DB"] = str(ledger)
        result = self._consume()
        self.assertEqual(result["verdict"], "UNKNOWN")

    def test_symlinked_registry_is_unknown(self) -> None:
        real = self.root / "registry.sqlite3"
        conn = registry.connect(str(real))
        conn.close()
        link = self.root / "registry-link.sqlite3"
        link.symlink_to(real)
        self.env["CAO_REGISTRY_DB"] = str(link)
        result = self._consume()
        self.assertEqual(result["verdict"], "UNKNOWN")

    def test_empty_registry_yields_a_fresh_quota(self) -> None:
        ledger = self.root / "registry.sqlite3"
        conn = registry.connect(str(ledger))
        conn.close()
        self.env["CAO_REGISTRY_DB"] = str(ledger)
        result = self._consume()
        self.assertEqual(result["verdict"], "SUPPORTED")
        self.assertEqual(result["consumed"], 0)


class QuotaDispatchTests(AuthorityDispatchCase):
    """A tight grant caps unique started READ teams across ticks/restarts."""

    def _queued(self, item_id: str) -> str:
        sid = self.admitted(item_id)
        intake_queue.enqueue(sid, repo=self.repo_real, source_ref="main",
                             env=self.env)
        return sid

    def test_two_submissions_with_tight_quota_start_one_unique_team(self) -> None:
        first = self._queued("quota-first")
        second = self._queued("quota-second")
        envelope_fn = lambda: self.envelope(max_dispatches=1)
        # Two independent ticks, each with a freshly built adapter (the restart
        # idiom: consumption is always re-derived from durable evidence).
        first_pass = intake_queue.tick(
            env=self.env, operator_authority=self.adapter(envelope_fn()))
        self.assertEqual(first_pass["items"][0]["decision"], "ADVANCE")
        team_id = first_pass["items"][0]["team_id"]
        second_pass = intake_queue.tick(
            env=self.env, operator_authority=self.adapter(envelope_fn()))
        second_item = [item for item in second_pass["items"]
                       if item["submission_id"] == second][0]
        self.assertEqual(second_item["status"], "HELD")
        self.assertEqual(len(team.list_teams(env=self.env)), 1)
        # The queue dispatch itself writes no Registry row at all, and the
        # freshly re-derived consumption refuses the second execution.
        conn = registry.connect_readonly(str(self.root / "registry.sqlite3"))
        rows = conn.execute(
            "SELECT COUNT(*) FROM claims").fetchone()[0]
        conn.close()
        self.assertEqual(rows, 0)
        verdict = self.adapter(envelope_fn()).evaluate(
            submission_id=second, plan=self.plan(second), env=self.env)
        self.assertEqual(verdict["code"], "authority_quota_exhausted")
        # No second READ team or started execution exists for submission 2.

    def test_quota_preflight_is_incomplete_only_until_evidence_is_complete(self) -> None:
        sid = self._queued("quota-unknown-first")
        envelope = self.envelope(max_dispatches=1)
        with mock.patch.object(intake_authority, "consumption",
                               return_value={"verdict": "UNKNOWN", "consumed": 0,
                                             "teams": [], "claims": [],
                                             "reason": "unreadable"}):
            report = intake_queue.tick(
                env=self.env, operator_authority=self.adapter(envelope))
        self.assertEqual(report["items"][0]["decision"], "HOLD")
        self.assertFalse(team.list_teams(env=self.env))

    def test_concurrent_passes_start_no_duplicate_execution(self) -> None:
        # While another pass holds the same durable dispatch lock, a tick with
        # a verified adapter mutates nothing (LOCKED) rather than racing a
        # duplicate team/claim through.
        sid = self._queued("quota-locked")
        envelope = self.envelope(max_dispatches=1)
        import fcntl
        directory = intake_queue.queue_dir(self.env)
        intake_queue._ensure_private_dir(directory)
        lock_fd = os.open(os.path.join(directory, ".lock"),
                          os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            report = intake_queue.tick(
                env=self.env, operator_authority=self.adapter(envelope))
            self.assertEqual(report["status"], "LOCKED")
            self.assertFalse(team.list_teams(env=self.env))
        finally:
            os.close(lock_fd)
        report = intake_queue.tick(
            env=self.env, operator_authority=self.adapter(envelope))
        self.assertEqual(report["items"][0]["decision"], "ADVANCE")
        self.assertEqual(len(team.list_teams(env=self.env)), 1)



    def _grant_env(self, document: dict, *, mode: int = 0o600,
                   directory: str | None = None) -> str:
        protected = Path(directory or (self.root / "protected"))
        protected.mkdir(mode=0o755, exist_ok=True)
        os.chmod(protected, 0o755)
        path = protected / intake_authority.DEFAULT_GRANT_BASENAME
        path.write_text(json.dumps(document), encoding="utf-8")
        os.chmod(path, mode)
        return str(protected)

    def _read(self, directory: str):
        return intake_authority.standing_authority(
            env=self.env, uid=os.getuid(), directory=directory,
            trusted_base=str(self.root))

    def test_absent_grant_holds_with_an_actionable_record(self) -> None:
        record = self._read(str(self.root / "nope"))
        self.assertEqual(record["verdict"], "UNAVAILABLE")
        self.assertIsNone(record["adapter"])
        self.assertIsNone(record["envelope"])
        self.assertIn("never creates or installs", record["integration"])

    def test_valid_grant_yields_a_supported_record_and_adapter(self) -> None:
        document = grant_document(scope={"repo": self.repo_real, "paths": ["a.txt"]})
        directory = self._grant_env(document)
        record = self._read(directory)
        self.assertEqual(record["verdict"], "SUPPORTED")
        self.assertIsNotNone(record["adapter"])
        self.assertEqual(record["envelope"]["operator"], "operator-one")

    def test_malformed_grant_is_refused(self) -> None:
        directory = self._grant_env(grant_document(model="bad model"))
        record = self._read(directory)
        self.assertEqual(record["verdict"], "REFUSED")
        self.assertIsNone(record["adapter"])

    def test_revoked_grant_is_refused_before_any_adapter(self) -> None:
        directory = self._grant_env(grant_document(revoked=True))
        record = self._read(directory)
        self.assertEqual(record["verdict"], "REFUSED")
        self.assertEqual(record["code"], "authority_revoked")
        self.assertIsNone(record["adapter"])

    def test_future_granted_at_is_refused(self) -> None:
        now = datetime.now(timezone.utc)
        directory = self._grant_env(grant_document(
            granted_at=(now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")))
        record = self._read(directory)
        self.assertEqual(record["verdict"], "REFUSED")
        self.assertEqual(record["code"], "authority_not_yet_valid")

    def test_expired_grant_is_refused(self) -> None:
        now = datetime.now(timezone.utc)
        directory = self._grant_env(grant_document(
            granted_at=(now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            expires_at=(now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")))
        record = self._read(directory)
        self.assertEqual(record["verdict"], "REFUSED")
        self.assertEqual(record["code"], "authority_expired")

    def test_missing_expiry_is_invalid(self) -> None:
        document = grant_document()
        del document["expires_at"]
        with self.assertRaises(intake_authority.AuthorityInvalid):
            intake_authority.validate_grant(document)

    def test_unbounded_ttl_is_invalid(self) -> None:
        # A de facto unbounded window is not a bounded validity interval.
        with self.assertRaises(intake_authority.AuthorityInvalid):
            intake_authority.validate_grant(grant_document(
                expires_at="2099-01-01T00:00:00Z"))

    def test_default_host_path_reports_honestly_and_writes_nothing(self) -> None:
        # The real host-shaped path is only read (never created or repaired):
        # with no provisioned root-owned grant the default authority simply
        # reports UNAVAILABLE and /etc/cao stays absent/unchanged.
        provisioned = os.path.isdir(intake_authority.DEFAULT_GRANT_DIR)
        record = intake_authority.standing_authority()
        self.assertIn(record["verdict"], ("UNAVAILABLE", "REFUSED", "SUPPORTED"))
        if not provisioned:
            self.assertEqual(record["verdict"], "UNAVAILABLE")
            self.assertIsNone(record["adapter"])

    def test_status_persists_nothing_and_builds_no_adapter(self) -> None:
        document = grant_document(scope={"repo": self.repo_real, "paths": ["a.txt"]})
        directory = self._grant_env(document)
        view = intake_authority.status(
            env=self.env, uid=os.getuid(), directory=directory,
            trusted_base=str(self.root))
        self.assertEqual(view["verdict"], "SUPPORTED")
        self.assertEqual(view["authority"], "standing")
        self.assertFalse(view["dispatch"])
        self.assertEqual(view["consumption"]["consumed"], 0)
        self.assertNotIn("adapter", view)
        self.assertFalse(team.list_teams(env=self.env))

    def test_authority_never_writes_a_ledger(self) -> None:
        directory = self._grant_env(
            grant_document(scope={"repo": self.repo_real, "paths": ["a.txt"]}))
        before = sorted(os.listdir(self.root))
        self._read(directory)
        self.assertEqual(sorted(os.listdir(self.root)), before)
        # No new CAO state directory or authority ledger is created.
        self.assertFalse((self.root / "state").exists())


class CliAuthorityTests(unittest.TestCase):
    def test_cli_reports_absent_authority_read_only(self) -> None:
        import contextlib
        import io

        from cao import intake_cli

        with tempfile.TemporaryDirectory() as directory:
            env = {"XDG_STATE_HOME": str(Path(directory) / "state"),
                   "XDG_CONFIG_HOME": str(Path(directory) / "config")}
            stream = io.StringIO()
            with mock.patch.dict(os.environ, env):
                with contextlib.redirect_stdout(stream):
                    code = intake_cli.main(["authority"])
            payload = json.loads(stream.getvalue())
        self.assertEqual(payload["schema"], intake_authority.STATUS_SCHEMA)
        self.assertIn(payload["verdict"], ("UNAVAILABLE", "REFUSED"))
        self.assertFalse(payload["dispatch"])
        self.assertEqual(code, 0)

    def test_cli_exposes_no_test_seams_for_authority(self) -> None:
        # The in-process test seams (uid/directory/trusted_base/clock) are
        # never reachable from the CLI or the environment.
        from cao import intake_cli
        parser = intake_cli.build_parser()
        args = parser.parse_args(["authority"])
        self.assertTrue(callable(args.handler))
        for seam in ("uid", "directory", "trusted_base"):
            self.assertIsNone(getattr(args, seam, None))


if __name__ == "__main__":
    unittest.main()
