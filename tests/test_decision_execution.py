"""Synthetic tests for the Batch B2d native model-backed READ execution binding.

Covers: the opt-in gate (``enabled = False`` reads nothing and creates no team,
prompt, plan, registry row or artifact); the authorized synthetic model-backed
plan (``dry_run``) that creates nothing; the real native READ team lifecycle --
started through the existing governed B6 owner path, completed at the bound HEAD
with its own terminal receipt, and closed with a released, correctly owned READ
Work Registry claim -- reaching an advisory ``PROPOSED`` through the B2b/B2c
native ``validate_result``; and every fail-closed hold: a missing, malformed or
expired protected standing grant; a non-zero-retention or out-of-vocab route; a
mixed READ+reserved request; a duplicated, echoed, dropped or forged terminal
receipt; a foreign team or a task on another revision; a foreign, missing or
still-active Registry claim; and a restarted or duplicated start.

It also proves the private-data guarantee: the raw admission id, the synthetic
private title and body token, and the host checkout path never appear in the
model-visible alias prompt, the manifest task prompt or the role instruction.

Nothing here is shipped and no real payload or model is used. The protected
grant is faked at the B8 reader's own seam, every value is synthetic, and the
whole lifecycle is hermetic under ``TMPDIR``.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from cao import (decision_agent as da, decision_execution, decision_intake,
                 decision_stage, intake, intake_authority, intake_handoff,
                 intake_queue, intake_triage, product_config, product_task,
                 registry, team)

_HAVE_GIT = shutil.which("git") is not None
SRC_ROOT = Path(__file__).resolve().parents[1] / "src"

EGRESS_MODULES = frozenset({
    "socket", "ssl", "urllib", "http", "http.client", "ftplib", "smtplib",
    "telnetlib", "xmlrpc", "asyncio", "requests", "httpx", "aiohttp", "urllib3",
})

READ_REQUEST = (
    "Read-only audit of the scoped source. Revalidate the default branch and "
    "report evidence. Never merge, deploy, publish or change credentials."
)
TITLE = "PRIVATE-TITLE-7c1e4b"
BODY_TOKEN = "PRIVATE-BODY-9f3a2b"
ITEM_ID = "private-item-b2d"
ROUTE = "example-provider/example-model"
DATA_CLASS = "synthetic"
NO_GRANT = object()
DEFAULT_GRANT = object()


def run_git(cwd, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
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


def tree(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}


class ExecutionCase(unittest.TestCase):
    """One isolated host: an admitted request, a checkout and a real ledger."""

    OWNER = "operator-one"

    def setUp(self) -> None:
        if not _HAVE_GIT:
            self.skipTest("git is required")
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.ledger = self.root / "registry.sqlite3"
        self.env = {
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "CAO_REGISTRY_DB": str(self.ledger),
        }
        registry.connect(str(self.ledger)).close()
        product_config.write_config(
            {
                "schema": product_config.SCHEMA,
                "provider": {"cli": "opencode", "model": "example/model"},
                "team_approval": {"mode": "chat"},
                "team_model_allowlist": [ROUTE],
            },
            product_config.config_path(self.env),
        )
        self.repo = make_repo(self.root)
        self.repo_real = os.path.realpath(str(self.repo))
        self.head = run_git(self.repo, "rev-parse", "HEAD").strip()
        self._seq = 0

    # -- fixtures ------------------------------------------------------------

    def submit(self, item_id: str, body: str) -> str:
        return intake.submit(
            {
                "schema": intake.REQUEST_SCHEMA,
                "item_id": item_id,
                "body": body,
                "title": TITLE,
                "source": "operator",
            },
            env=self.env,
        )["submission"]["submission_id"]

    def admitted(self, item_id: str | None = None, body: str = READ_REQUEST,
                 *, triage: bool = True) -> str:
        if item_id is None:
            self._seq += 1
            item_id = f"{ITEM_ID}-{self._seq}"
        sid = self.submit(item_id, body)
        if triage:
            intake_triage.triage(sid, env=self.env)
        return sid

    def grant_document(self, **overrides) -> dict:
        now = datetime.now(timezone.utc)
        document = {
            "schema": intake_authority.AUTHORITY_SCHEMA,
            "operator": "operator-one",
            "owner": self.OWNER,
            "role": "reader",
            "model": ROUTE,
            "privacy_qualified": True,
            "scope": {"repo": self.repo_real, "paths": ["a.txt"]},
            "source": {"ref": "main"},
            "submissions": None,
            "max_dispatches": 3,
            "granted_at": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at": (now + timedelta(days=29)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "revoked": False,
        }
        document.update(overrides)
        return document

    @contextlib.contextmanager
    def protected(self, grant=DEFAULT_GRANT):
        if grant is NO_GRANT:
            with mock.patch.object(
                intake_authority,
                "read_protected_grant",
                side_effect=intake_authority.AuthorityUnavailable(
                    "no protected standing-authority grant was established"
                ),
            ):
                yield
            return
        if grant is DEFAULT_GRANT:
            grant = self.grant_document()
        with mock.patch.object(
            intake_authority, "read_protected_grant", return_value=grant
        ):
            yield

    def qualify(self, **overrides) -> dict:
        base = {
            "route": ROUTE,
            "allowlist": [ROUTE],
            "data_class": DATA_CLASS,
            "qualified_data_classes": [DATA_CLASS],
            "provider_qualified": True,
            "retention": "zero",
        }
        base.update(overrides)
        return base

    def start(self, sid: str, *, grant=DEFAULT_GRANT, **over) -> dict:
        kwargs = {
            "env": self.env,
            "repo": self.repo_real,
            "source_ref": "main",
            "route_qualification": self.qualify(),
            "enabled": True,
        }
        kwargs.update(over)
        with self.protected(grant):
            return decision_execution.start_read_team(sid, **kwargs)

    def validate(self, sid: str, *, grant=DEFAULT_GRANT, **over) -> dict:
        kwargs = {
            "env": self.env,
            "repo": self.repo_real,
            "source_ref": "main",
            "route_qualification": self.qualify(),
            "enabled": True,
        }
        kwargs.update(over)
        with self.protected(grant):
            return decision_execution.validate_read_result(sid, **kwargs)

    def plan_record(self, sid: str) -> dict:
        return intake_handoff.handoff_plan(
            sid, repo=self.repo_real, source_ref="main", env=self.env, dry_run=True
        )["plan"]

    def alias_for(self, sid: str, head: str | None = None) -> str:
        return decision_intake.pseudonymous_alias(
            submission_id=sid, owner=self.OWNER,
            repository=f"{self.OWNER}/{self.repo.name}",
            head=head or self.head, plan_id=self.plan_record(sid)["plan_id"],
        )

    def ref_for(self, sid: str) -> dict:
        plan = self.plan_record(sid)
        repository = f"{self.OWNER}/{self.repo.name}"
        return {
            "location": f"urn:cao:source:{repository}@{self.head}",
            "digest": intake_handoff._binding_digest(
                {"plan_id": plan["plan_id"], "binding": plan["binding"]}),
            "cutoff": "2026-01-01T00:00:00Z",
            "head": self.head,
            "scope": {"owner": self.OWNER, "repository": repository,
                      "data_class": DATA_CLASS},
            "verdict": "SUPPORTED",
        }

    def teams(self) -> list[dict]:
        return team.list_teams(env=self.env)

    # -- native lifecycle ----------------------------------------------------

    def complete_task(
        self, sid: str, team_id: str, *, head: str | None = None,
        status: str = "complete", echo_receipt: bool = False,
        drop_receipt: bool = False, duplicate_receipt: bool = False,
        extra_tail: bool = False, plan_body: dict | None = None,
        tag_submission: str | None = None,
    ) -> dict:
        """Set the bound team's single task to a terminal native checkpoint."""

        head = head or self.head
        record = team._read(team_id, self.env)
        task = record["spec"]["tasks"][0]
        slot = record["slots"][task["id"]]
        if status != "complete":
            slot.update(status=status, execution_id=None, prompt=None, head=None,
                        output=None, error=None)
            team._save(record, self.env)
            return {"receipt": None, "execution_id": None, "task_id": task["id"]}
        slot.update(
            status="complete", head=head, prompt=task["prompt"], error=None,
            execution_id=team._execution_id(
                team_id, task["id"], slot["phase"], slot["round"]
            ),
        )
        receipt = team._receipt_for(slot["execution_id"], slot["prompt"])
        tag = tag_submission if tag_submission is not None else self.alias_for(sid, head)
        if plan_body is not None:
            text = (
                f'{decision_stage.OPEN_TAG} submission="{tag}" task="{task["id"]}">\n'
                f"{json.dumps(plan_body)}\n"
                f"{decision_stage.CLOSE_TAG}\n"
            )
        else:
            text = (
                "CAO advisory READ review (bounded).\n"
                f"Submission: {tag}\n"
                "Findings: the scoped source was inspected at the bound revision.\n"
            )
        if duplicate_receipt:
            text += f"{receipt}\n"
        if not drop_receipt:
            text += f"{receipt}\n"
        if extra_tail:
            text += "trailing prose after the receipt\n"
        slot["output"] = text
        if echo_receipt:
            slot["prompt"] = f"{slot['prompt']}\n{receipt}\n"
        team._save(record, self.env)
        return {"receipt": receipt, "execution_id": slot["execution_id"],
                "task_id": task["id"]}

    def release_claim(self, team_id: str, execution_id: str, head: str, *,
                      owner: str | None = None, mode: str = "READ",
                      outcome: str = "complete", released: bool = True,
                      released_head: str | None = None) -> None:
        run_id = "cao-team-" + team_id
        conn = registry.connect(str(self.ledger))
        try:
            conn.execute(
                "INSERT INTO works (run_id, work_key, issue, repo, paths, "
                "components, objective, created_at) VALUES (?, ?, '', ?, '', "
                "'', '', ?)",
                (run_id, execution_id, self.repo_real, "2026-01-01T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO claims (run_id, work_key, issue, owner, mode, "
                "branch, worktree, claimed_at, released_at, outcome, "
                "released_head) VALUES (?, ?, '', ?, ?, '', '', ?, ?, ?, ?)",
                (
                    run_id, execution_id,
                    owner if owner is not None else product_task.OWNER,
                    mode, "2026-01-01T00:00:00Z",
                    "2026-01-01T00:01:00Z" if released else None,
                    outcome,
                    ((head if released_head is None else released_head)
                     if released else ""),
                ),
            )
        finally:
            conn.close()

    def plan_body(self, sid: str, action: str = "READ", *,
                  digest: str | None = None) -> dict:
        ref = self.ref_for(sid)
        subtasks = [{"id": "s1", "summary": "inspect the scoped source",
                     "action_class": "READ"}]
        tests: list[str] = []
        approvals = ["operator_read_gate"]
        if action != "READ":
            subtasks.append({"id": "s2", "summary": "apply the change",
                             "action_class": action})
            tests = ["unit"]
            approvals = list(da.REQUIRED_APPROVALS[action])
        return {
            "schema": da.PLAN_SCHEMA,
            "source_refs": [{"location": ref["location"],
                             "digest": digest or ref["digest"]}],
            "plan": "Inspect the scoped source and report findings.",
            "subtasks": subtasks,
            "dependencies": [{"id": "s1", "depends_on": []}],
            "owner": "operator-one-role",
            "capability": {"kind": "read", "performer": "example-reader"},
            "privacy_and_model_qualification": {
                "data_class": DATA_CLASS, "route": ROUTE},
            "confidence": 0.5,
            "uncertainty": "the example evidence may be incomplete",
            "action_class": action,
            "tests": tests,
            "approval_requirements": approvals,
        }

    def started_team(self, sid: str, **over) -> dict:
        started = self.start(sid, **over)
        self.assertEqual(
            started["status"], decision_execution.STARTED,
            msg=f"start held: {started['code']} {started['reason']}",
        )
        return started

    def finished_team(self, sid: str, *, action: str = "READ", **over) -> dict:
        started = self.started_team(sid, **over)
        done = self.complete_task(sid, started["team_id"],
                                  plan_body=self.plan_body(sid, action))
        self.release_claim(started["team_id"], done["execution_id"], self.head)
        return started


class GateTests(ExecutionCase):
    """Default-off and the authorized plan create nothing until enabled."""

    def test_disabled_reads_nothing_and_creates_nothing(self) -> None:
        sid = self.admitted()
        before = tree(self.root)
        started = decision_execution.start_read_team(sid)
        validated = decision_execution.validate_read_result(sid)
        for result in (started, validated):
            self.assertEqual(result["status"], "HELD")
            self.assertEqual(result["code"], "model_stage_disabled")
            self.assertIsNone(result["alias"])
            self.assertIsNone(result["prompt"])
            self.assertIs(result["dispatch"], False)
            self.assertEqual(result["authority"], "none")
            self.assertEqual(result["effects"], [])
        self.assertEqual(self.teams(), [])
        self.assertEqual(tree(self.root), before)

    def test_authorized_synthetic_plan_dry_run_creates_nothing(self) -> None:
        sid = self.admitted()
        result = self.start(sid, dry_run=True)
        self.assertEqual(result["status"], decision_execution.AUTHORIZED)
        self.assertIs(result["dispatch"], False)
        self.assertEqual(result["authority"], "none")
        self.assertEqual(result["submission_id"], sid)
        self.assertEqual(result["alias"], self.alias_for(sid))
        self.assertEqual(result["plan_id"], self.plan_record(sid)["plan_id"])
        self.assertEqual(result["plan"]["status"], "AUTHORIZED")
        self.assertEqual(self.teams(), [])
        self.assertEqual(result["effects"], [])

    def test_missing_protected_grant_holds_and_creates_no_team(self) -> None:
        sid = self.admitted()
        result = self.start(sid, grant=NO_GRANT)
        self.assertEqual(result["status"], "HELD")
        self.assertEqual(result["code"], "standing_grant_unavailable")
        self.assertIs(result["dispatch"], False)
        self.assertEqual(self.teams(), [])

    def test_malformed_protected_grant_is_refused(self) -> None:
        sid = self.admitted()
        result = self.start(sid, grant=self.grant_document(owner="two words"))
        self.assertEqual(result["status"], "HELD")
        self.assertEqual(result["code"], "standing_grant_refused")
        self.assertEqual(self.teams(), [])

    def test_expired_protected_grant_is_refused(self) -> None:
        sid = self.admitted()
        past = (datetime.now(timezone.utc) - timedelta(days=3)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        result = self.start(sid, grant=self.grant_document(
            granted_at="2026-01-01T00:00:00Z", expires_at=past))
        self.assertEqual(result["status"], "HELD")
        self.assertEqual(result["code"], "standing_grant_refused")
        self.assertEqual(self.teams(), [])

    def test_revoked_protected_grant_is_refused(self) -> None:
        sid = self.admitted()
        result = self.start(sid, grant=self.grant_document(revoked=True))
        self.assertEqual(result["status"], "HELD")
        self.assertEqual(result["code"], "standing_grant_refused")
        self.assertEqual(self.teams(), [])

    def test_nonzero_retention_route_holds(self) -> None:
        sid = self.admitted()
        result = self.start(sid, route_qualification=self.qualify(retention="standard"))
        self.assertEqual(result["status"], "HELD")
        self.assertEqual(result["code"], "unqualified_retention")
        self.assertEqual(self.teams(), [])

    def test_unqualified_route_holds(self) -> None:
        sid = self.admitted()
        result = self.start(sid, route_qualification=self.qualify(allowlist=[]))
        self.assertEqual(result["status"], "HELD")
        self.assertEqual(result["code"], "unqualified_route")
        self.assertEqual(self.teams(), [])

    def test_mixed_read_and_reserved_request_holds(self) -> None:
        sid = self.admitted(
            body="Read-only audit of the source, then one squash merge of PR 7.")
        result = self.start(sid)
        self.assertEqual(result["status"], "HELD")
        self.assertEqual(result["code"], "mixed_effect_and_read_request")
        self.assertEqual(self.teams(), [])


class PrivateDataTests(ExecutionCase):
    """No private admission data ever reaches a model-visible prompt."""

    def test_manifest_prompt_and_role_carry_no_private_data(self) -> None:
        sid = self.admitted(body=READ_REQUEST + f" Note: {BODY_TOKEN}.")
        result = self.started_team(sid)
        record = team._read(result["team_id"], self.env)
        task_prompt = record["spec"]["tasks"][0]["prompt"]
        role_prompt = next(iter(record["spec"]["roles"].values()))["instruction"]
        model_visible = " ".join((result["prompt"], task_prompt, role_prompt))
        for private in (sid, TITLE, BODY_TOKEN, ITEM_ID, "PRIVATE", self.repo_real):
            with self.subTest(private=private):
                self.assertNotIn(private, model_visible)
        self.assertNotIn("://", model_visible)
        self.assertIn(result["alias"], model_visible)
        self.assertEqual(result["team_id"], "intake-read-" + sid[:16])
        self.assertEqual(result["task_id"], "read-" + sid[:12])

    def test_alias_binds_the_exact_private_evidence(self) -> None:
        sid = self.admitted()
        result = self.started_team(sid)
        self.assertTrue(result["alias"].startswith(decision_intake.ALIAS_PREFIX))
        self.assertNotEqual(result["alias"], sid)
        self.assertEqual(result["alias"], self.alias_for(sid))
        with self.assertRaises(da.DecisionHeld):
            decision_intake.pseudonymous_alias(
                submission_id="not-hex", owner=self.OWNER,
                repository=f"{self.OWNER}/{self.repo.name}", head=self.head,
                plan_id="b" * 64)


class LifecycleTests(ExecutionCase):
    """The full native READ lifecycle reaches advisory PROPOSED or held."""

    def test_real_native_read_team_reaches_proposed(self) -> None:
        sid = self.admitted()
        started = self.finished_team(sid)
        result = self.validate(sid)
        self.assertEqual(result["status"], "PROPOSED")
        self.assertIsNone(result["code"])
        self.assertEqual(result["alias"], started["alias"])
        self.assertEqual(result["team_id"], started["team_id"])
        self.assertEqual(result["task_id"], started["task_id"])
        self.assertEqual(result["plan_id"], started["plan_id"])
        self.assertEqual(result["stage"]["stage"]["action_class"], "READ")
        self.assertEqual(result["stage"]["stage"]["plan"]["schema"], da.PLAN_SCHEMA)
        self.assertIs(result["dispatch"], False)
        self.assertEqual(result["authority"], "none")
        self.assertEqual(result["effects"], [])
        self.assertEqual(result["egress"], "none")
        self.assertIs(result["advisory_only"], True)
        self.assertIs(result["detail"]["receipt_verified"], True)
        self.assertEqual(result["detail"]["claim"], "released_read_claim")

    def test_model_produced_write_and_reserved_effects_stay_held(self) -> None:
        for action in ("WRITE", "RESERVED"):
            with self.subTest(action=action):
                sid = self.admitted()
                started = self.started_team(sid)
                done = self.complete_task(
                    sid, started["team_id"], plan_body=self.plan_body(sid, action))
                self.release_claim(started["team_id"], done["execution_id"], self.head)
                result = self.validate(sid)
                self.assertEqual(result["status"], "HELD")
                self.assertEqual(result["code"], "review_only")
                self.assertIs(result["dispatch"], False)
                self.assertEqual(result["authority"], "none")
                self.assertEqual(result["effects"], [])

    def test_duplicate_and_restarted_start_never_re_prompts(self) -> None:
        sid = self.admitted()
        first = self.started_team(sid)
        record_before = team._read(first["team_id"], self.env)
        second = self.start(sid)
        self.assertEqual(second["status"], "HELD")
        self.assertEqual(second["code"], "already_started")
        self.assertIs(second["dispatch"], False)
        record_after = team._read(first["team_id"], self.env)
        self.assertEqual(record_before["tick_count"], record_after["tick_count"])
        self.assertEqual(len(self.teams()), 1)
        slot = record_after["slots"][first["task_id"]]
        self.assertIsNone(slot["execution_id"])

    def test_disabled_validate_after_a_real_team_is_still_held(self) -> None:
        sid = self.admitted()
        self.finished_team(sid)
        result = decision_execution.validate_read_result(sid)
        self.assertEqual(result["code"], "model_stage_disabled")
        self.assertIsNone(result["stage"])


class ReceiptTests(ExecutionCase):
    """The terminal receipt and Registry claim proof fail closed."""

    def _validate_after(self, sid: str, **over) -> dict:
        release = over.pop("release", True)
        over.setdefault("plan_body", self.plan_body(sid))
        started = self.started_team(sid)
        done = self.complete_task(sid, started["team_id"], **over)
        if done["execution_id"] is not None and release:
            self.release_claim(started["team_id"], done["execution_id"], self.head)
        return self.validate(sid)

    def test_missing_terminal_receipt_is_held(self) -> None:
        result = self._validate_after(self.admitted(), drop_receipt=True)
        self.assertEqual(result["status"], "HELD")
        self.assertEqual(result["code"], "native_receipt_missing")

    def test_duplicated_terminal_receipt_is_held(self) -> None:
        result = self._validate_after(self.admitted(), duplicate_receipt=True)
        self.assertEqual(result["status"], "HELD")
        self.assertEqual(result["code"], "receipt_ambiguous")

    def test_receipt_echoed_by_its_own_prompt_is_held(self) -> None:
        result = self._validate_after(self.admitted(), echo_receipt=True)
        self.assertEqual(result["status"], "HELD")
        self.assertEqual(result["code"], "receipt_echoed")

    def test_trailing_prose_after_the_receipt_is_held(self) -> None:
        result = self._validate_after(self.admitted(), extra_tail=True)
        self.assertEqual(result["status"], "HELD")
        self.assertEqual(result["code"], "receipt_ambiguous")

    def test_missing_registry_claim_is_held(self) -> None:
        result = self._validate_after(self.admitted(), release=False)
        self.assertEqual(result["status"], "HELD")
        self.assertEqual(result["code"], "registry_claim_missing")

    def test_active_registry_claim_is_held(self) -> None:
        sid = self.admitted()
        started = self.started_team(sid)
        done = self.complete_task(sid, started["team_id"],
                                  plan_body=self.plan_body(sid))
        self.release_claim(started["team_id"], done["execution_id"], self.head,
                           released=False)
        result = self.validate(sid)
        self.assertEqual(result["status"], "HELD")
        self.assertEqual(result["code"], "registry_claim_active")

    def test_foreign_registry_claim_owner_is_held(self) -> None:
        sid = self.admitted()
        started = self.started_team(sid)
        done = self.complete_task(sid, started["team_id"],
                                  plan_body=self.plan_body(sid))
        self.release_claim(started["team_id"], done["execution_id"], self.head,
                           owner="someone-else")
        result = self.validate(sid)
        self.assertEqual(result["status"], "HELD")
        self.assertEqual(result["code"], "registry_claim_foreign")

    def test_forged_tagged_submission_is_held(self) -> None:
        sid = self.admitted()
        result = self._validate_after(
            sid, tag_submission="synthetic-" + "0" * 40)
        self.assertEqual(result["status"], "HELD")
        self.assertEqual(result["code"], "submission_mismatch")
        self.assertIs(result["dispatch"], False)


class ForeignAndDriftTests(ExecutionCase):
    """A foreign team, a drifted HEAD and an uncited team are never accepted."""

    def test_task_on_another_revision_is_held(self) -> None:
        sid = self.admitted()
        started = self.started_team(sid)
        other = "a" * 40
        done = self.complete_task(sid, started["team_id"], head=other,
                                  plan_body=self.plan_body(sid))
        self.release_claim(started["team_id"], done["execution_id"], other)
        result = self.validate(sid)
        self.assertEqual(result["status"], "HELD")
        self.assertEqual(result["code"], "source_head_drift")

    def test_uncited_forged_team_is_held(self) -> None:
        sid = self.admitted()
        plan = self.plan_record(sid)
        manifest = intake_handoff._manifest(
            plan,
            {"role": "reader", "model": ROUTE, "repo": self.repo_real,
             "paths": ["a.txt"]},
            citation="synthetic-" + "0" * 40,
        )
        config = product_config.load_config(product_config.config_path(self.env))
        team.initialize(manifest, env=self.env, config=config, approved=True)
        team.set_desired(manifest["team_id"], "running", env=self.env)
        done = self.complete_task(sid, manifest["team_id"],
                                  plan_body=self.plan_body(sid))
        self.release_claim(manifest["team_id"], done["execution_id"], self.head)
        result = self.validate(sid)
        self.assertEqual(result["status"], "HELD")
        self.assertEqual(result["code"], "not_read_only")

    def test_foreign_write_manifest_is_never_a_read_result(self) -> None:
        sid = self.admitted()
        plan = self.plan_record(sid)
        manifest = intake_handoff._manifest(
            plan,
            {"role": "reader", "model": ROUTE, "repo": self.repo_real,
             "paths": ["a.txt"]},
            citation=self.alias_for(sid),
        )
        task = manifest["tasks"][0]
        task["mode"] = "write"
        task["write_scope"] = ["a.txt"]
        manifest["max_writers"] = 1
        config = product_config.load_config(product_config.config_path(self.env))
        team.initialize(manifest, env=self.env, config=config, approved=True)
        team.set_desired(manifest["team_id"], "running", env=self.env)
        result = self.validate(sid)
        self.assertEqual(result["status"], "HELD")
        self.assertEqual(result["code"], "not_read_only")


class NativePromptTests(ExecutionCase):
    """The exact verified B2a/B2b tagged decision-plan prompt reaches the worker."""

    def test_worker_prompt_carries_the_exact_tagged_decision_plan(self) -> None:
        sid = self.admitted(body=READ_REQUEST + f" Note: {BODY_TOKEN}.")
        started = self.started_team(sid)
        record = team._read(started["team_id"], self.env)
        task = record["spec"]["tasks"][0]
        prompt = task["prompt"]
        ref = self.ref_for(sid)
        for fragment in (
            "location=" + ref["location"],
            "digest=" + ref["digest"],
            "head=" + ref["head"],
            "cutoff=",
            "owner=" + ref["scope"]["owner"],
            "repository=" + ref["scope"]["repository"],
            "data_class=" + ref["scope"]["data_class"],
            da.PLAN_SCHEMA,
            f'<cao-plan-result submission="{started["alias"]}" '
            f'task="{started["task_id"]}">',
            decision_stage.CLOSE_TAG,
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, prompt)
        # The manifest carries the exact prompt the verified B2b stage built.
        with self.protected():
            stage_prompt = decision_intake.prepare_read_prompt(
                sid, env=self.env, repo=self.repo_real, source_ref="main",
                route_qualification=self.qualify(), enabled=True)["prompt"]
        self.assertEqual(stage_prompt, prompt)
        # The assembled worker prompt asks for the tagged plan block before the
        # native terminal receipt instruction, and never carries private data.
        slot = record["slots"][started["task_id"]]
        assembled = team._prompt(task, record["spec"], slot, record["slots"])
        self.assertIn("<cao-plan-result", assembled)
        self.assertIn("CAO_TEAM_RESULT:", assembled)
        self.assertLess(assembled.index("<cao-plan-result"),
                        assembled.index("CAO_TEAM_RESULT:"))
        for private in (sid, TITLE, BODY_TOKEN, ITEM_ID, self.repo_real):
            with self.subTest(private=private):
                self.assertNotIn(private, assembled)


class AuthoritySeamTests(ExecutionCase):
    """Authority is the protected adapter only; a caller dictionary is refused."""

    def test_caller_supplied_grant_mapping_is_refused_and_creates_no_team(self) -> None:
        sid = self.admitted()
        result = intake_handoff.handoff_plan(
            sid, repo=self.repo_real, source_ref="main", env=self.env,
            operator_authority={"authenticated": True,
                                "grant": {"read_only": True}},
            citation=self.alias_for(sid))
        self.assertEqual(result["status"], "REFUSED")
        self.assertEqual(self.teams(), [])


class QuotaTests(ExecutionCase):
    """One shared, durable B8 quota covers the B2d and B6/B7 routes alike."""

    def _envelope(self) -> dict:
        return self.grant_document(max_dispatches=1)

    def _adapter(self, envelope: dict):
        return intake_authority.OperatorAuthority(
            intake_authority.validate_grant(envelope), env=self.env)

    def test_shared_quota_refuses_the_second_alias_and_id_citing_dispatch(self) -> None:
        first = self.start(self.admitted(), grant=self._envelope())
        self.assertEqual(first["status"], decision_execution.STARTED)
        self.assertEqual(len(self.teams()), 1)
        second = self.start(self.admitted(), grant=self._envelope())
        self.assertEqual(second["status"], "HELD")
        self.assertEqual(second["code"], "dispatch_refused")
        self.assertEqual(len(self.teams()), 1)
        # A B6/B7 dispatch through the identical protected adapter is refused by
        # the same shared quota: the alias-citing team is counted for it.
        third = intake_handoff.handoff_plan(
            self.admitted(), repo=self.repo_real, source_ref="main",
            env=self.env, operator_authority=self._adapter(self._envelope()))
        self.assertEqual(third["status"], "REFUSED")
        self.assertEqual(len(self.teams()), 1)

    def test_incomplete_quota_evidence_holds(self) -> None:
        with mock.patch.object(
            intake_authority, "consumption",
            return_value={"verdict": "UNKNOWN", "consumed": 0, "teams": [],
                          "claims": [], "reason": "unreadable"}):
            result = self.start(self.admitted(), grant=self._envelope())
        self.assertEqual(result["status"], "HELD")
        self.assertEqual(self.teams(), [])

    def test_a_held_queue_lock_refuses_the_admission(self) -> None:
        import fcntl
        sid = self.admitted()
        started = self.started_team(sid)
        other = self.admitted()
        directory = intake_queue.queue_dir(self.env)
        intake_queue._ensure_private_dir(directory)
        lock_fd = os.open(os.path.join(directory, ".lock"),
                          os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            blocked = self.start(other)
            self.assertEqual(blocked["status"], "HELD")
            self.assertEqual(blocked["code"], "dispatch_busy")
        finally:
            os.close(lock_fd)
        self.assertEqual(len(self.teams()), 1)
        self.assertEqual(started["team_id"], self.teams()[0]["team_id"])
        self.assertEqual(self.start(other)["status"], decision_execution.STARTED)

    def test_concurrent_dispatches_never_oversubscribe_the_quota(self) -> None:
        import threading
        envelope = self._envelope()
        sids = [self.admitted(), self.admitted()]
        results: dict[int, dict] = {}
        barrier = threading.Barrier(2)

        def run(index: int) -> None:
            barrier.wait()
            try:
                results[index] = decision_execution.start_read_team(
                    sids[index], env=self.env, repo=self.repo_real,
                    source_ref="main", route_qualification=self.qualify(),
                    enabled=True)
            except Exception as exc:  # noqa: BLE001 - recorded, not raised
                results[index] = {"status": "ERROR", "code": type(exc).__name__}

        with self.protected(envelope):
            threads = [threading.Thread(target=run, args=(i,)) for i in (0, 1)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        started = [r for r in results.values()
                   if r.get("status") == decision_execution.STARTED]
        self.assertEqual(len(started), 1, msg=f"results: {results}")
        self.assertEqual(len(self.teams()), 1)


class ModuleShapeTests(unittest.TestCase):
    """The module is opt-in, stdlib-only, egress-free and no second executor."""

    def source(self) -> str:
        return (SRC_ROOT / "cao" / "decision_execution.py").read_text(
            encoding="utf-8")

    def test_signatures_default_to_disabled(self) -> None:
        for name in ("start_read_team", "validate_read_result"):
            parameter = inspect.signature(
                getattr(decision_execution, name)
            ).parameters["enabled"]
            self.assertIs(parameter.default, False)

    def test_public_surface_is_bounded(self) -> None:
        self.assertEqual(
            sorted(decision_execution.__all__),
            ["AUTHORIZED", "EXECUTION_SCHEMA", "STARTED", "start_read_team",
             "validate_read_result"],
        )

    def test_no_executor_tick_registry_write_or_shell(self) -> None:
        source = self.source()
        for forbidden in (
            "subprocess", "os.system", "os.popen", "eval(", "exec(", "socket",
            "urlopen", "spawn", "Popen", "check_output", "registry.connect(",
            "registry.propose", "registry.claim(", "registry.release",
            "registry.write_gate", "registry.handoff", "registry.record_",
            "team.initialize(", "team.set_desired(", "team.tick(",
            "team.tick_all(", "team.resume(", "team.retry_read(",
            "product_task.run", "state.write", "open_private_dir(",
            "write_private_record(", "os.makedirs", "os.chmod",
            "intake_queue.enqueue(", "intake_queue.tick(",
            "product_daemon", "threading",
            "asyncio", "Timer", "while True",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)
        self.assertIn("intake_handoff.handoff_plan(", source)
        self.assertIn("intake_queue._queue_lock(", source)

    def test_imports_are_stdlib_and_cao_only(self) -> None:
        tree_ = ast.parse(self.source())
        for node in ast.walk(tree_):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(
                    node.func.id, {"eval", "exec", "__import__", "compile"})
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.ImportFrom) and node.level:
                    continue
                names = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                for name in names:
                    top = name.split(".")[0]
                    self.assertNotIn(top, EGRESS_MODULES, name)
                    self.assertIn(top, sys.stdlib_module_names, name)


if __name__ == "__main__":
    unittest.main()
