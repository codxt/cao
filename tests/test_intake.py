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
