"""Synthetic tests for durable, private intake admission (B1).

Covers duplicates and idempotency, conflicting content, corrupt and
unknown-schema state, concurrency, restart persistence, the CLI surface,
secret redaction, strict bounded JSON and the security properties (private
permissions, symlink refusal, no work item or ledger created).

Nothing here is shipped. Every value is synthetic.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from cao import intake, intake_cli

REPO_ROOT = Path(__file__).resolve().parents[1]


def _doc(item_id: str = "item-1", body: str = "hello world", **extra) -> dict:
    document = {"schema": intake.REQUEST_SCHEMA, "item_id": item_id, "body": body}
    document.update(extra)
    return document


class _StateFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.env = {"XDG_STATE_HOME": os.path.join(self._temp.name, "state")}
        self.state_root = Path(self.env["XDG_STATE_HOME"]) / "cao"

    def _write(self, name: str, payload) -> Path:
        path = Path(self._temp.name) / name
        text = payload if isinstance(payload, str) else json.dumps(payload)
        path.write_text(text, encoding="utf-8")
        return path


class AdmissionTests(_StateFixture):
    def test_submit_admits_with_durable_id_and_private_permissions(self) -> None:
        result = intake.submit(_doc(), env=self.env)
        self.assertEqual(result["status"], "ADMITTED")
        self.assertFalse(result["duplicate"])
        submission = result["submission"]
        self.assertEqual(submission["state"], "ADMITTED")
        self.assertEqual(submission["record_type"], "intake_submission")
        self.assertNotIn("body", submission)

        path = intake.submission_path(submission["submission_id"], self.env)
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(os.stat(intake.intake_dir(self.env)).st_mode), 0o700
        )
        # ``makedirs`` only modes the leaf, so the CAO state root the intake
        # directory sits under must be hardened too.
        self.assertEqual(stat.S_IMODE(os.stat(self.state_root).st_mode), 0o700)

    def test_duplicate_item_and_fingerprint_collapses(self) -> None:
        first = intake.submit(_doc(), env=self.env)
        second = intake.submit(_doc(), env=self.env)
        self.assertEqual(second["status"], "DUPLICATE")
        self.assertTrue(second["duplicate"])
        self.assertEqual(
            first["submission"]["submission_id"],
            second["submission"]["submission_id"],
        )
        self.assertEqual(len(intake.list_submissions(self.env)), 1)

    def test_conflicting_content_is_refused_and_original_kept(self) -> None:
        first = intake.submit(_doc(body="first"), env=self.env)
        with self.assertRaises(intake.IntakeConflict):
            intake.submit(_doc(body="second"), env=self.env)
        stored = intake.read_submission(
            first["submission"]["submission_id"], self.env
        )
        self.assertEqual(stored["body"], "first")

    def test_restart_preserves_submission_and_idempotency(self) -> None:
        first = intake.submit(_doc(body="durable"), env=self.env)
        sid = first["submission"]["submission_id"]
        # A fresh call is what a restarted process does; no in-memory cache.
        self.assertEqual(intake.read_submission(sid, self.env)["body"], "durable")
        self.assertEqual(
            intake.submit(_doc(body="durable"), env=self.env)["status"],
            "DUPLICATE",
        )

    def test_admission_creates_no_work_item_or_ledger(self) -> None:
        intake.submit(_doc(), env=self.env)
        self.assertEqual(sorted(os.listdir(self.state_root)), ["intake"])
        source = Path(intake.__file__).read_text(encoding="utf-8")
        self.assertNotIn("registry", source)

    def test_unknown_submission_id_fails_closed(self) -> None:
        with self.assertRaises(intake.IntakeUnavailable):
            intake.read_submission("0" * 64, self.env)
        with self.assertRaises(intake.IntakeInvalid):
            intake.read_submission("not-a-hex-id", self.env)


class CorruptStateTests(_StateFixture):
    def test_corrupt_record_raises_and_is_never_empty(self) -> None:
        submitted = intake.submit(_doc(), env=self.env)
        path = intake.submission_path(
            submitted["submission"]["submission_id"], self.env
        )
        Path(path).write_text("{not valid json", encoding="utf-8")
        with self.assertRaises(intake.IntakeStateError):
            intake.list_submissions(self.env)
        with self.assertRaises(intake.IntakeStateError):
            intake.read_submission(submitted["submission"]["submission_id"], self.env)

    def test_unknown_schema_raises(self) -> None:
        submitted = intake.submit(_doc(), env=self.env)
        path = intake.submission_path(
            submitted["submission"]["submission_id"], self.env
        )
        record = json.loads(Path(path).read_text(encoding="utf-8"))
        record["schema"] = "cao.intake.submission.v99"
        Path(path).write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(intake.IntakeStateError):
            intake.list_submissions(self.env)

    def test_unexpected_field_raises(self) -> None:
        submitted = intake.submit(_doc(), env=self.env)
        path = intake.submission_path(
            submitted["submission"]["submission_id"], self.env
        )
        record = json.loads(Path(path).read_text(encoding="utf-8"))
        record["injected"] = True
        Path(path).write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(intake.IntakeStateError):
            intake.read_submission(submitted["submission"]["submission_id"], self.env)


class ConcurrencyTests(_StateFixture):
    def test_concurrent_same_item_yields_one_record(self) -> None:
        count = 8
        barrier = threading.Barrier(count)
        results: list[dict] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait(timeout=5)
                results.append(intake.submit(_doc(), env=self.env))
            except BaseException as exc:  # noqa: BLE001 - surfaced in the test
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), count)
        self.assertEqual(len(intake.list_submissions(self.env)), 1)
        admitted = [r for r in results if r["status"] == "ADMITTED"]
        ids = {r["submission"]["submission_id"] for r in results}
        self.assertEqual(len(admitted), 1)
        self.assertEqual(len(ids), 1)

    def test_concurrent_distinct_items_are_all_kept(self) -> None:
        count = 10
        barrier = threading.Barrier(count)
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=5)
                intake.submit(_doc(item_id=f"item-{index}"), env=self.env)
            except BaseException as exc:  # noqa: BLE001 - surfaced in the test
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(index,)) for index in range(count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(intake.list_submissions(self.env)), count)


class SecretHandlingTests(_StateFixture):
    def test_obvious_secrets_are_redacted_before_storage(self) -> None:
        aws_key = "AKIA" + "A" * 16
        github_token = "ghp_" + "b" * 36
        body = (
            f"access_key={aws_key}\n"
            f"Authorization: Bearer {github_token}\n"
            "password=hunter2secret\n"
        )
        result = intake.submit(_doc(body=body), env=self.env)
        self.assertTrue(result["submission"]["body_redacted"])
        stored = intake.read_submission(
            result["submission"]["submission_id"], self.env
        )
        self.assertNotIn(aws_key, stored["body"])
        self.assertNotIn(github_token, stored["body"])
        self.assertNotIn("hunter2secret", stored["body"])
        self.assertIn("[REDACTED]", stored["body"])

    def test_public_metadata_excludes_body(self) -> None:
        result = intake.submit(_doc(body="private body"), env=self.env)
        record = intake.read_submission(
            result["submission"]["submission_id"], self.env
        )
        view = intake.public_view(record)
        self.assertNotIn("body", view)
        self.assertNotIn("private body", json.dumps(view))

    def test_bearer_value_is_fully_redacted_without_a_suffix(self) -> None:
        token = "abc123def456ghi789jkl"
        body = f"Authorization: Bearer {token}\n"
        stored = intake.read_submission(
            intake.submit(_doc(body=body), env=self.env)["submission"][
                "submission_id"
            ],
            self.env,
        )
        self.assertNotIn(token, stored["body"])
        self.assertNotIn("abc123", stored["body"])
        self.assertIn("[REDACTED]", stored["body"])

    def test_authorization_with_non_bearer_scheme_is_redacted(self) -> None:
        credential = "dXNlcjpwYXNzd29yZA=="
        body = f"Authorization: Basic {credential}\n"
        stored = intake.read_submission(
            intake.submit(_doc(body=body), env=self.env)["submission"][
                "submission_id"
            ],
            self.env,
        )
        self.assertNotIn(credential, stored["body"])

    def test_quoted_assignment_is_redacted_whole(self) -> None:
        body = 'password="hunter two with spaces"\nsecret: \'a b c d\'\n'
        stored = intake.read_submission(
            intake.submit(_doc(body=body), env=self.env)["submission"][
                "submission_id"
            ],
            self.env,
        )
        self.assertNotIn("hunter", stored["body"])
        self.assertNotIn("two with spaces", stored["body"])
        self.assertNotIn("a b c d", stored["body"])

    def test_json_style_credential_assignment_is_redacted(self) -> None:
        body = '{"authorization":"Basic dXNlcjpwYXNz","note":"ok"}'
        stored = intake.read_submission(
            intake.submit(_doc(body=body), env=self.env)["submission"][
                "submission_id"
            ],
            self.env,
        )
        self.assertNotIn("dXNlcjpwYXNz", stored["body"])

    def test_credential_shaped_item_id_is_refused(self) -> None:
        for item_id in (
            "password=hunter2",
            "token:ghp_" + "b" * 36,
            "sk-" + "a" * 24,
            "Bearer abc123def456",
        ):
            with self.subTest(item_id=item_id):
                with self.assertRaises(intake.IntakeInvalid):
                    intake.submit(_doc(item_id=item_id), env=self.env)

    def test_single_quoted_credential_keys_are_redacted_whole(self) -> None:
        body = (
            "{'password': 'synthetic quoted value'}\n"
            "'token'='synthetic-token-value'\n"
        )
        stored = intake.read_submission(
            intake.submit(_doc(body=body), env=self.env)["submission"][
                "submission_id"
            ],
            self.env,
        )
        self.assertNotIn("synthetic quoted value", stored["body"])
        self.assertNotIn("synthetic-token-value", stored["body"])
        self.assertIn("[REDACTED]", stored["body"])

    def test_single_quoted_credential_shaped_item_id_is_refused(self) -> None:
        with self.assertRaises(intake.IntakeInvalid):
            intake.submit(
                _doc(item_id="'token'='synthetic-token-value'"), env=self.env
            )

    def test_public_view_never_carries_a_raw_credential(self) -> None:
        secret = "synthetic-token-value"
        result = intake.submit(
            _doc(
                title="'token'='%s'" % secret,
                source="'password'='%s'" % secret,
                evidence_refs=[{"ref": "'token'='%s'" % secret}],
            ),
            env=self.env,
        )
        record = intake.read_submission(
            result["submission"]["submission_id"], self.env
        )
        self.assertNotIn(secret, json.dumps(intake.public_view(record)))
        self.assertNotIn(secret, json.dumps(record))


class StrictInputTests(_StateFixture):
    def _load(self, name: str, payload) -> dict:
        return intake.load_submission_file(self._write(name, payload))

    def test_oversized_input_refused(self) -> None:
        path = Path(self._temp.name) / "big.json"
        path.write_text("x" * (intake.MAX_SUBMISSION_BYTES + 1), encoding="utf-8")
        with self.assertRaises(intake.IntakeInvalid):
            intake.load_submission_file(path)

    def test_duplicate_json_key_refused(self) -> None:
        with self.assertRaises(intake.IntakeInvalid):
            self._load(
                "dup.json",
                '{"schema": "%s", "item_id": "a", "item_id": "b", "body": ""}'
                % intake.REQUEST_SCHEMA,
            )

    def test_non_finite_number_refused(self) -> None:
        with self.assertRaises(intake.IntakeInvalid):
            self._load(
                "nan.json",
                '{"schema": "%s", "item_id": "a", "body": NaN}'
                % intake.REQUEST_SCHEMA,
            )

    def test_non_object_refused(self) -> None:
        with self.assertRaises(intake.IntakeInvalid):
            self._load("array.json", "[1, 2, 3]")

    def test_symlink_refused(self) -> None:
        real = self._write("real.json", _doc())
        link = Path(self._temp.name) / "link.json"
        os.symlink(real, link)
        with self.assertRaises(intake.IntakeError):
            intake.load_submission_file(link)

    def test_shape_violations_refused(self) -> None:
        cases = {
            "missing_schema": {"item_id": "a", "body": "x"},
            "wrong_schema": {"schema": "nope", "item_id": "a", "body": "x"},
            "unknown_field": {
                "schema": intake.REQUEST_SCHEMA,
                "item_id": "a",
                "body": "x",
                "extra": 1,
            },
            "empty_item_id": {"schema": intake.REQUEST_SCHEMA, "item_id": "", "body": "x"},
            "spacey_item_id": {
                "schema": intake.REQUEST_SCHEMA,
                "item_id": "a b",
                "body": "x",
            },
            "missing_body": {"schema": intake.REQUEST_SCHEMA, "item_id": "a"},
            "body_not_string": {
                "schema": intake.REQUEST_SCHEMA,
                "item_id": "a",
                "body": 5,
            },
            "refs_not_list": {
                "schema": intake.REQUEST_SCHEMA,
                "item_id": "a",
                "body": "x",
                "evidence_refs": {},
            },
            "ref_unknown_field": {
                "schema": intake.REQUEST_SCHEMA,
                "item_id": "a",
                "body": "x",
                "evidence_refs": [{"ref": "r", "extra": 1}],
            },
        }
        for label, document in cases.items():
            with self.subTest(case=label), self.assertRaises(intake.IntakeInvalid):
                intake.normalize_submission(document)

    def test_oversized_body_and_refs_refused(self) -> None:
        with self.assertRaises(intake.IntakeInvalid):
            intake.normalize_submission(
                _doc(body="x" * (intake.MAX_BODY_CHARS + 1))
            )
        with self.assertRaises(intake.IntakeInvalid):
            intake.normalize_submission(
                _doc(
                    evidence_refs=[
                        {"ref": f"ref-{i}"}
                        for i in range(intake.MAX_EVIDENCE_REFS + 1)
                    ]
                )
            )


class EvidenceRefTests(_StateFixture):
    def test_evidence_refs_are_retained(self) -> None:
        refs = [
            {"ref": "urn:example:observation:1", "fingerprint": "sha256:" + "c" * 64},
            {"ref": "urn:example:observation:2"},
        ]
        result = intake.submit(_doc(evidence_refs=refs), env=self.env)
        stored = intake.read_submission(
            result["submission"]["submission_id"], self.env
        )
        self.assertEqual(stored["evidence_refs"], refs)


class StoredRecordTamperTests(_StateFixture):
    """F1: a stored record is re-derived from its bytes before every read.

    Tampering the body, the id, an evidence ref or the schema fails closed;
    show, list and the duplicate path all verify the same bindings.
    """

    def setUp(self) -> None:
        super().setUp()
        result = intake.submit(_doc(body="original"), env=self.env)
        self.sid = result["submission"]["submission_id"]
        self.path = Path(intake.submission_path(self.sid, self.env))

    def _record(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, record) -> None:
        self.path.write_text(json.dumps(record), encoding="utf-8")

    def test_tampered_body_fails_closed_on_show_and_list(self) -> None:
        record = self._record()
        record["body"] = "tampered"
        self._write(record)
        with self.assertRaises(intake.IntakeStateError):
            intake.read_submission(self.sid, self.env)
        with self.assertRaises(intake.IntakeStateError):
            intake.list_submissions(self.env)

    def test_tampered_item_id_fails_closed(self) -> None:
        record = self._record()
        record["item_id"] = "different-item"
        self._write(record)
        with self.assertRaises(intake.IntakeStateError):
            intake.read_submission(self.sid, self.env)

    def test_tampered_evidence_ref_fails_closed(self) -> None:
        record = self._record()
        record["evidence_refs"] = [{"ref": "urn:example:injected"}]
        self._write(record)
        with self.assertRaises(intake.IntakeStateError):
            intake.list_submissions(self.env)

    def test_tampered_schema_fails_closed(self) -> None:
        record = self._record()
        record["schema"] = "cao.intake.submission.v99"
        self._write(record)
        with self.assertRaises(intake.IntakeStateError):
            intake.read_submission(self.sid, self.env)

    def test_duplicate_path_recomputes_fingerprint(self) -> None:
        record = self._record()
        record["body"] = "tampered"
        self._write(record)
        # The same item with different content reaches the duplicate path; it
        # must verify the stored record rather than trust its fingerprint.
        with self.assertRaises(intake.IntakeStateError):
            intake.submit(_doc(body="other"), env=self.env)

    def test_duplicate_json_key_in_record_fails_closed(self) -> None:
        self.path.write_text(
            '{"schema": "%s", "schema": "%s"}'
            % (intake.SUBMISSION_SCHEMA, intake.SUBMISSION_SCHEMA),
            encoding="utf-8",
        )
        with self.assertRaises(intake.IntakeStateError):
            intake.read_submission(self.sid, self.env)

    def test_non_finite_number_in_record_fails_closed(self) -> None:
        self.path.write_text('{"n": NaN}', encoding="utf-8")
        with self.assertRaises(intake.IntakeStateError):
            intake.read_submission(self.sid, self.env)

    def test_oversized_record_fails_closed(self) -> None:
        self.path.write_bytes(b"x" * (intake.MAX_RECORD_BYTES + 1))
        with self.assertRaises(intake.IntakeStateError):
            intake.read_submission(self.sid, self.env)


class EvidenceRefCanonicalTests(_StateFixture):
    """Nested evidence refs must already be canonical in the stored bytes.

    Canonicalizing before fingerprinting must not launder a tampered record: an
    injected ``fingerprint: null`` or an unknown nested field is refused on
    ``show``, ``list`` and the duplicate path rather than returned as part of
    the raw record. Absent and null are not equivalent.
    """

    def setUp(self) -> None:
        super().setUp()
        result = intake.submit(
            _doc(evidence_refs=[{"ref": "urn:example:observation:1"}]),
            env=self.env,
        )
        self.sid = result["submission"]["submission_id"]
        self.path = Path(intake.submission_path(self.sid, self.env))

    def _rewrite_refs(self, refs) -> None:
        record = json.loads(self.path.read_text(encoding="utf-8"))
        record["evidence_refs"] = refs
        self.path.write_text(json.dumps(record), encoding="utf-8")

    def test_injected_null_fingerprint_is_refused_on_show_list_duplicate(
        self,
    ) -> None:
        self._rewrite_refs(
            [{"ref": "urn:example:observation:1", "fingerprint": None}]
        )
        with self.assertRaises(intake.IntakeStateError):
            intake.read_submission(self.sid, self.env)
        with self.assertRaises(intake.IntakeStateError):
            intake.list_submissions(self.env)
        with self.assertRaises(intake.IntakeStateError):
            intake.submit(
                _doc(evidence_refs=[{"ref": "urn:example:observation:1"}]),
                env=self.env,
            )

    def test_injected_unknown_nested_field_is_refused(self) -> None:
        self._rewrite_refs([{"ref": "urn:example:observation:1", "extra": 1}])
        with self.assertRaises(intake.IntakeStateError):
            intake.read_submission(self.sid, self.env)
        with self.assertRaises(intake.IntakeStateError):
            intake.list_submissions(self.env)

    def test_absent_fingerprint_remains_valid(self) -> None:
        record = intake.read_submission(self.sid, self.env)
        self.assertEqual(
            record["evidence_refs"], [{"ref": "urn:example:observation:1"}]
        )


class PrivatePathSafetyTests(_StateFixture):
    """F3: records and directories are protected from symlinks, non-regular
    files, unsafe modes and foreign targets, while legitimate private state
    stays usable."""

    def test_record_symlink_fails_closed(self) -> None:
        result = intake.submit(_doc(), env=self.env)
        sid = result["submission"]["submission_id"]
        path = Path(intake.submission_path(sid, self.env))
        target = Path(self._temp.name) / "elsewhere.json"
        target.write_text("{}", encoding="utf-8")
        path.unlink()
        os.symlink(target, path)
        with self.assertRaises(intake.IntakeError):
            intake.read_submission(sid, self.env)

    def test_record_with_unsafe_mode_fails_closed(self) -> None:
        result = intake.submit(_doc(), env=self.env)
        sid = result["submission"]["submission_id"]
        path = Path(intake.submission_path(sid, self.env))
        os.chmod(path, 0o644)
        with self.assertRaises(intake.IntakeStateError):
            intake.read_submission(sid, self.env)

    def test_record_replaced_by_directory_fails_closed(self) -> None:
        result = intake.submit(_doc(), env=self.env)
        sid = result["submission"]["submission_id"]
        path = Path(intake.submission_path(sid, self.env))
        path.unlink()
        path.mkdir()
        with self.assertRaises(intake.IntakeStateError):
            intake.read_submission(sid, self.env)

    def test_intake_dir_symlink_fails_closed(self) -> None:
        foreign = Path(self._temp.name) / "foreign"
        foreign.mkdir()
        cao = Path(self.env["XDG_STATE_HOME"]) / "cao"
        cao.mkdir(parents=True)
        os.symlink(foreign, cao / "intake")
        with self.assertRaises(intake.IntakeError):
            intake.submit(_doc(), env=self.env)

    def test_symlinked_parent_is_refused_and_not_chmod_ed(self) -> None:
        foreign = Path(self._temp.name) / "foreign"
        foreign.mkdir()
        os.chmod(foreign, 0o755)
        state_home = Path(self.env["XDG_STATE_HOME"])
        state_home.mkdir(parents=True)
        os.symlink(foreign, state_home / "cao")
        with self.assertRaises(intake.IntakeError):
            intake.submit(_doc(), env=self.env)
        # The foreign target was never chmod'ed through the link.
        self.assertEqual(stat.S_IMODE(foreign.stat().st_mode), 0o755)

    def test_legitimate_private_state_remains_usable(self) -> None:
        result = intake.submit(_doc(body="usable"), env=self.env)
        sid = result["submission"]["submission_id"]
        self.assertEqual(intake.read_submission(sid, self.env)["body"], "usable")
        self.assertEqual(len(intake.list_submissions(self.env)), 1)
        self.assertEqual(
            stat.S_IMODE(Path(intake.submission_path(sid, self.env)).stat().st_mode),
            0o600,
        )
        self.assertEqual(
            stat.S_IMODE(Path(intake.intake_dir(self.env)).stat().st_mode), 0o700
        )

    def test_symlinked_cao_parent_fails_closed_on_list_and_read(self) -> None:
        real_state = Path(self._temp.name) / "real"
        result = intake.submit(_doc(), env={"XDG_STATE_HOME": str(real_state)})
        sid = result["submission"]["submission_id"]
        state_home = Path(self._temp.name) / "linked_state"
        state_home.mkdir()
        os.symlink(real_state / "cao", state_home / "cao")
        env = {"XDG_STATE_HOME": str(state_home)}
        with self.assertRaises(intake.IntakeError):
            intake.list_submissions(env)
        with self.assertRaises(intake.IntakeError):
            intake.read_submission(sid, env)

    def test_dangling_cao_parent_fails_closed_not_empty(self) -> None:
        state_home = Path(self.env["XDG_STATE_HOME"])
        state_home.mkdir(parents=True)
        os.symlink(state_home / "nowhere", state_home / "cao")
        with self.assertRaises(intake.IntakeError):
            intake.list_submissions(self.env)

    def test_unsafe_cao_parent_mode_fails_closed_on_list_and_read(self) -> None:
        result = intake.submit(_doc(), env=self.env)
        sid = result["submission"]["submission_id"]
        os.chmod(Path(intake.intake_dir(self.env)).parent, 0o755)
        with self.assertRaises(intake.IntakeStateError):
            intake.list_submissions(self.env)
        with self.assertRaises(intake.IntakeStateError):
            intake.read_submission(sid, self.env)

    def test_symlinked_cao_parent_target_is_never_modified_by_reads(self) -> None:
        foreign = Path(self._temp.name) / "foreign_state"
        (foreign / "intake").mkdir(parents=True)
        os.chmod(foreign, 0o700)
        os.chmod(foreign / "intake", 0o700)
        state_home = Path(self._temp.name) / "linked_state2"
        state_home.mkdir()
        os.symlink(foreign, state_home / "cao")
        env = {"XDG_STATE_HOME": str(state_home)}
        with self.assertRaises(intake.IntakeError):
            intake.list_submissions(env)
        # A refused read never chmod'ed or otherwise touched the target.
        self.assertEqual(stat.S_IMODE(foreign.stat().st_mode), 0o700)


class SpecialFileRecordTests(_StateFixture):
    """A FIFO substituted for a record must be refused promptly, never hung on.

    ``_open_record_at`` opens with ``O_NONBLOCK``; without it, opening a FIFO
    blocks before ``fstat`` can reject a non-regular file, so the reader hangs.
    The probe runs in a bounded subprocess, so a hang is a test failure instead
    of a stuck run.
    """

    def _probe(self, call: str, timeout: float = 20.0):
        script = "\n".join(
            [
                "import sys",
                "sys.path.insert(0, %r)" % str(REPO_ROOT / "src"),
                "from cao import intake",
                "try:",
                "    " + call,
                "except intake.IntakeError as exc:",
                "    print('REFUSED', exc.code)",
                "    raise SystemExit(0)",
                "raise SystemExit(3)",
            ]
        )
        env = dict(os.environ)
        env.update(self.env)
        return subprocess.run(
            [sys.executable, "-B", "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )

    def setUp(self) -> None:
        super().setUp()
        directory = intake.intake_dir(self.env)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(Path(directory).parent, 0o700)
        os.chmod(directory, 0o700)
        self.sid = "a" * 64
        self.fifo = os.path.join(directory, self.sid + ".json")
        os.mkfifo(self.fifo, 0o600)
        self.addCleanup(lambda: os.path.exists(self.fifo) and os.unlink(self.fifo))

    def test_fifo_record_is_refused_promptly_on_read(self) -> None:
        proc = self._probe(
            "intake.read_submission(%r, %r)" % (self.sid, self.env)
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("REFUSED INTAKE_STATE", proc.stdout)

    def test_fifo_record_is_refused_promptly_on_list(self) -> None:
        proc = self._probe("intake.list_submissions(%r)" % (self.env,))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("REFUSED INTAKE_STATE", proc.stdout)


class ListAbsenceTests(_StateFixture):
    """F4: ``list`` returns ``[]`` only for a genuinely absent directory."""

    def test_absent_directory_returns_empty(self) -> None:
        self.assertEqual(intake.list_submissions(self.env), [])

    def test_directory_path_that_is_a_file_raises(self) -> None:
        state_home = Path(self.env["XDG_STATE_HOME"])
        state_home.mkdir(parents=True)
        (state_home / "cao").write_text("not a directory", encoding="utf-8")
        with self.assertRaises(intake.IntakeError):
            intake.list_submissions(self.env)

    def test_directory_that_is_a_symlink_raises(self) -> None:
        foreign = Path(self._temp.name) / "foreign"
        (foreign / "intake").mkdir(parents=True)
        cao = Path(self.env["XDG_STATE_HOME"]) / "cao"
        cao.mkdir(parents=True)
        os.symlink(foreign / "intake", cao / "intake")
        with self.assertRaises(intake.IntakeError):
            intake.list_submissions(self.env)

    def test_directory_with_unsafe_mode_raises(self) -> None:
        intake.submit(_doc(), env=self.env)
        os.chmod(intake.intake_dir(self.env), 0o755)
        with self.assertRaises(intake.IntakeStateError):
            intake.list_submissions(self.env)


class DurabilityTests(_StateFixture):
    """F6: a write is fsynced through the containing directory before it is
    acknowledged; a failure never falsely acknowledges and an honest retry
    reconciles the record."""

    def test_directory_fsync_failure_never_acknowledges_and_retry_reconciles(
        self,
    ) -> None:
        real_fsync_dir = intake._fsync_dir
        state = {"first": True}

        def flaky(directory: str) -> None:
            if state["first"]:
                state["first"] = False
                raise OSError("simulated directory fsync failure")
            real_fsync_dir(directory)

        with mock.patch.object(intake, "_fsync_dir", side_effect=flaky):
            with self.assertRaises(intake.IntakeUnavailable):
                intake.submit(_doc(), env=self.env)

        # The name was linked but never acknowledged. The honest retry finds it
        # and reconciles to one durable record.
        retry = intake.submit(_doc(), env=self.env)
        self.assertEqual(retry["status"], "DUPLICATE")
        self.assertEqual(len(intake.list_submissions(self.env)), 1)

    def test_record_fsync_failure_leaves_no_record_and_retry_admits(self) -> None:
        with mock.patch("os.fsync", side_effect=OSError("simulated fsync failure")):
            with self.assertRaises(intake.IntakeUnavailable):
                intake.submit(_doc(), env=self.env)
        # Nothing was linked, so a retry is an honest first admission.
        retry = intake.submit(_doc(), env=self.env)
        self.assertEqual(retry["status"], "ADMITTED")
        self.assertEqual(len(intake.list_submissions(self.env)), 1)

    def test_duplicate_path_syncs_directory_before_acknowledging(self) -> None:
        intake.submit(_doc(), env=self.env)
        real_fsync_dir = intake._fsync_dir
        calls = {"n": 0}

        def counting(directory: str) -> None:
            calls["n"] += 1
            real_fsync_dir(directory)

        with mock.patch.object(intake, "_fsync_dir", side_effect=counting):
            duplicate = intake.submit(_doc(), env=self.env)
        # The duplicate/retry path must sync the containing directory too, so a
        # link that was not yet durable cannot be acknowledged as a duplicate.
        self.assertEqual(calls["n"], 1)
        self.assertEqual(duplicate["status"], "DUPLICATE")

    def test_repeated_directory_sync_failures_never_falsely_acknowledge(
        self,
    ) -> None:
        real_fsync_dir = intake._fsync_dir
        state = {"failures": 2}

        def flaky(directory: str) -> None:
            if state["failures"] > 0:
                state["failures"] -= 1
                raise OSError("simulated directory fsync failure")
            real_fsync_dir(directory)

        with mock.patch.object(intake, "_fsync_dir", side_effect=flaky):
            # First write links the name but fails its directory sync; the
            # retry hits the duplicate path and its sync fails too. Neither may
            # be acknowledged as ADMITTED or DUPLICATE.
            for _ in range(2):
                with self.assertRaises(intake.IntakeUnavailable):
                    intake.submit(_doc(), env=self.env)
            # Only once the directory sync succeeds is the duplicate returned.
            final = intake.submit(_doc(), env=self.env)
        self.assertEqual(final["status"], "DUPLICATE")
        self.assertEqual(len(intake.list_submissions(self.env)), 1)


class AncestryDurabilityTests(_StateFixture):
    """F1: every directory created on first submit has its parent entry fsynced
    before an admission is acknowledged, and a failure to sync a required
    ancestor never acknowledges durable success."""

    def test_created_ancestry_is_fsynced_before_the_record_link(self) -> None:
        # Pre-create the XDG state root so exactly the two CAO-owned directories
        # (the CAO state dir and the intake dir) are created by the submit.
        Path(self.env["XDG_STATE_HOME"]).mkdir(parents=True)
        events: list[str] = []
        real_fsync = os.fsync
        real_link = os.link

        def fsync(fd: int) -> None:
            kind = "dir-fsync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file-fsync"
            events.append(kind)
            real_fsync(fd)

        def link(src, dst, **kwargs):
            events.append("link")
            return real_link(src, dst, **kwargs)

        with mock.patch("os.fsync", side_effect=fsync):
            with mock.patch("os.link", side_effect=link):
                result = intake.submit(_doc(), env=self.env)

        self.assertEqual(result["status"], "ADMITTED")
        link_at = events.index("link")
        # Both newly created directories have their parent entry fsynced before
        # the record is linked into place.
        self.assertEqual(events[:link_at].count("dir-fsync"), 2)

    def test_ancestry_fsync_failure_refuses_before_any_record(self) -> None:
        real_fsync = os.fsync
        state = {"fail": True}

        def flaky(fd: int) -> None:
            if state["fail"] and stat.S_ISDIR(os.fstat(fd).st_mode):
                state["fail"] = False
                raise OSError("simulated ancestry fsync failure")
            real_fsync(fd)

        with mock.patch("os.fsync", side_effect=flaky):
            with self.assertRaises(intake.IntakeUnavailable):
                intake.submit(_doc(), env=self.env)

        # The refusal hit an ancestry sync before the record was linked, so
        # nothing was admitted: an unsynced ancestry never yields a durable
        # acknowledgement.
        self.assertEqual(intake.list_submissions(self.env), [])
        retry = intake.submit(_doc(), env=self.env)
        self.assertEqual(retry["status"], "ADMITTED")
        self.assertEqual(len(intake.list_submissions(self.env)), 1)

    def test_existing_ancestry_is_not_resynced(self) -> None:
        intake.submit(_doc(), env=self.env)
        real_fsync = os.fsync
        dir_syncs = {"n": 0}

        def counting(fd: int) -> None:
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                dir_syncs["n"] += 1
            real_fsync(fd)

        with mock.patch("os.fsync", side_effect=counting):
            duplicate = intake.submit(_doc(), env=self.env)

        self.assertEqual(duplicate["status"], "DUPLICATE")
        # Only the leaf directory is synced on the duplicate path; the already
        # existing ancestry is not re-synced.
        self.assertEqual(dir_syncs["n"], 1)


class ParentSubstitutionTests(_StateFixture):
    """F2: after a path has been validated, a same-user swap of a parent path
    component must not redirect a record into a substitute tree. The write,
    the link, the cleanup and the read-back all use the validated descriptor."""

    def _substitute(self) -> Path:
        substitute = Path(self._temp.name) / "substitute"
        (substitute / "intake").mkdir(parents=True, exist_ok=True)
        os.chmod(substitute, 0o700)
        os.chmod(substitute / "intake", 0o700)
        return substitute

    def _submit_with_swap(self, swap) -> dict:
        real_open = os.open
        state = {"swapped": False}

        def swapping_open(path, flags, *args, **kwargs):
            # Swap the validated path component at the moment the private temp
            # record is created -- after validation, before the write lands.
            if (
                not state["swapped"]
                and ".intake-" in os.fspath(path)
                and flags & os.O_CREAT
            ):
                state["swapped"] = True
                swap()
            return real_open(path, flags, *args, **kwargs)

        with mock.patch("os.open", side_effect=swapping_open):
            try:
                result = intake.submit(_doc(body="private body"), env=self.env)
            except intake.IntakeError as exc:
                result = exc
        self.assertTrue(state["swapped"], "the temp-file creation was not intercepted")
        return result

    def test_swapped_cao_parent_receives_no_record(self) -> None:
        substitute = self._substitute()
        cao = self.state_root
        moved = Path(self._temp.name) / "cao-moved"

        result = self._submit_with_swap(
            lambda: (os.rename(cao, moved), os.symlink(substitute, cao))
        )

        # The substituted tree must never receive the record.
        self.assertEqual(list(substitute.rglob("*.json")), [])
        self.assertIsInstance(result, dict, f"submit failed: {result!r}")
        self.assertEqual(result["status"], "ADMITTED")
        sid = result["submission"]["submission_id"]
        # The record lives in the directory that was actually validated, under
        # its new name, and the admission is therefore real, not false.
        self.assertTrue((moved / "intake" / (sid + ".json")).is_file())

    def test_swapped_intake_leaf_receives_no_record(self) -> None:
        substitute = self._substitute()
        leaf = Path(intake.intake_dir(self.env))
        moved = Path(self._temp.name) / "intake-moved"

        result = self._submit_with_swap(
            lambda: (os.rename(leaf, moved), os.symlink(substitute / "intake", leaf))
        )

        self.assertEqual(list(substitute.rglob("*.json")), [])
        self.assertIsInstance(result, dict, f"submit failed: {result!r}")
        self.assertEqual(result["status"], "ADMITTED")
        sid = result["submission"]["submission_id"]
        self.assertTrue((moved / (sid + ".json")).is_file())


class CliTests(_StateFixture):
    def _run(self, argv: list[str]) -> tuple[int, dict]:
        stream = io.StringIO()
        with mock.patch.dict(os.environ, self.env):
            with contextlib.redirect_stdout(stream):
                code = intake_cli.main(argv)
        return code, json.loads(stream.getvalue())

    def test_submit_list_show_and_pull(self) -> None:
        path = self._write("submission.json", _doc(body="cli body"))
        code, payload = self._run(["submit", "--file", str(path)])
        self.assertEqual(code, intake_cli.EXIT_OK)
        self.assertEqual(payload["status"], "ADMITTED")
        sid = payload["submission"]["submission_id"]

        code, listing = self._run(["list"])
        self.assertEqual(code, intake_cli.EXIT_OK)
        self.assertEqual(listing["schema"], intake_cli.LIST_SCHEMA)
        self.assertEqual(listing["count"], 1)
        self.assertNotIn("body", listing["submissions"][0])

        code, shown = self._run(["show", sid])
        self.assertEqual(code, intake_cli.EXIT_OK)
        self.assertEqual(shown["submission"]["body"], "cli body")

        code, pulled = self._run(["pull"])
        self.assertEqual(code, intake_cli.EXIT_UNAVAILABLE)
        self.assertEqual(pulled["status"], "UNAVAILABLE")
        self.assertEqual(pulled["error"]["code"], "NO_INBOUND_TRANSPORT")

    def test_bad_file_reports_unavailable(self) -> None:
        code, payload = self._run(["submit", "--file", str(Path(self._temp.name) / "missing.json")])
        self.assertEqual(code, intake_cli.EXIT_UNAVAILABLE)
        self.assertEqual(payload["status"], "UNAVAILABLE")

    def test_missing_command_is_usage(self) -> None:
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(io.StringIO()):
            code = intake_cli.main([])
        self.assertEqual(code, intake_cli.EXIT_USAGE)

    def test_product_cli_routes_intake_group(self) -> None:
        from cao import product_cli

        path = self._write("submission.json", _doc(body="routed"))
        stream = io.StringIO()
        with mock.patch.dict(os.environ, self.env):
            with contextlib.redirect_stdout(stream):
                code = product_cli.main(["intake", "submit", "--file", str(path)])
        self.assertEqual(code, product_cli.EXIT_OK)
        self.assertEqual(json.loads(stream.getvalue())["status"], "ADMITTED")


if __name__ == "__main__":
    unittest.main()
