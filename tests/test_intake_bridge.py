"""Synthetic end-to-end tests for the inbound intake bridge (B2).

Everything here is synthetic: no real marketplace, no network, no credential and
no private payload. The tests drive the real CLI/transport path -- a submission
document on a byte stream -- and assert the security properties the bridge
claims: admission and reconciliation, collision refusal, injection inertness,
and fail-closed unsupported/deferred transports.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cao import intake, intake_bridge, intake_cli, product_cli

TRANSPORT = intake_bridge.TRANSPORT_MARKETPLACE


def _doc(item_id: str = "item-1", body: str = "hello world", **extra) -> dict:
    document = {"schema": intake.REQUEST_SCHEMA, "item_id": item_id, "body": body}
    document.update(extra)
    return document


def _payload(**kwargs) -> bytes:
    return json.dumps(_doc(**kwargs)).encode("utf-8")


class _FakeStdin:
    """A stand-in for ``sys.stdin`` that exposes a binary ``.buffer``."""

    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)


class _CliFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.env = {"XDG_STATE_HOME": os.path.join(self._temp.name, "state")}
        self.state_root = Path(self.env["XDG_STATE_HOME"]) / "cao"

    def _run(self, argv: list[str], stdin: bytes = b"") -> tuple[int, dict]:
        stream = io.StringIO()
        with mock.patch.dict(os.environ, self.env):
            with mock.patch("sys.stdin", _FakeStdin(stdin)):
                with contextlib.redirect_stdout(stream):
                    code = product_cli.main(argv)
        return code, json.loads(stream.getvalue())

    def _pull(self, transport: str | None = TRANSPORT, stdin: bytes = b""):
        argv = ["intake", "pull"]
        if transport is not None:
            argv += ["--transport", transport]
        return self._run(argv, stdin=stdin)


class ContractTests(unittest.TestCase):
    def test_contract_names_one_marketplace_transport_and_defers_github(self) -> None:
        contract = intake_bridge.bridge_contract()
        self.assertEqual(contract["schema"], intake_bridge.CONTRACT_SCHEMA)
        self.assertEqual(contract["registered_transports"], [TRANSPORT])
        self.assertIn("github-issue", contract["deferred_transports"])
        self.assertIs(contract["executes"], False)
        self.assertIs(contract["starts_services"], False)
        self.assertEqual(contract["max_bytes"], intake.MAX_SUBMISSION_BYTES)

    def test_transports_command_reports_the_contract(self) -> None:
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = intake_cli.main(["transports"])
        self.assertEqual(code, intake_cli.EXIT_OK)
        self.assertEqual(json.loads(stream.getvalue())["schema"],
                         intake_bridge.CONTRACT_SCHEMA)


class EndToEndBridgeTests(_CliFixture):
    def test_marketplace_stdin_admits_then_retry_reconciles(self) -> None:
        code, first = self._pull(stdin=_payload(item_id="i1", body="payload"))
        self.assertEqual(code, product_cli.EXIT_OK)
        self.assertEqual(first["status"], "ADMITTED")
        self.assertEqual(first["transport"], TRANSPORT)
        sid = first["submission"]["submission_id"]
        fingerprint = first["submission"]["fingerprint"]

        # The sender lost the answer (ambiguous send) and retried the same item.
        code, second = self._pull(stdin=_payload(item_id="i1", body="payload"))
        self.assertEqual(code, product_cli.EXIT_OK)
        self.assertEqual(second["status"], "DUPLICATE")
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["submission"]["submission_id"], sid)
        self.assertEqual(second["submission"]["fingerprint"], fingerprint)

        code, listing = self._run(["intake", "list"])
        self.assertEqual(listing["count"], 1)

    def test_distinct_items_are_kept_separately(self) -> None:
        self._pull(stdin=_payload(item_id="a"))
        self._pull(stdin=_payload(item_id="b"))
        _code, listing = self._run(["intake", "list"])
        self.assertEqual(listing["count"], 2)

    def test_file_input_uses_the_same_strict_path(self) -> None:
        path = Path(self._temp.name) / "submission.json"
        path.write_bytes(_payload(item_id="file-item"))
        code, payload = self._run(
            ["intake", "pull", "--transport", TRANSPORT, "--file", str(path)]
        )
        self.assertEqual(code, product_cli.EXIT_OK)
        self.assertEqual(payload["status"], "ADMITTED")

    def test_private_permissions_on_the_bridge_path(self) -> None:
        self._pull(stdin=_payload())
        record_dir = Path(intake.intake_dir(self.env))
        self.assertEqual(stat.S_IMODE(record_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.state_root.stat().st_mode), 0o700)
        records = list(record_dir.glob("*.json"))
        self.assertEqual(len(records), 1)
        self.assertEqual(stat.S_IMODE(records[0].stat().st_mode), 0o600)


class CollisionTests(_CliFixture):
    def test_same_item_different_content_is_refused(self) -> None:
        _code, first = self._pull(stdin=_payload(item_id="c1", body="original"))
        code, payload = self._pull(stdin=_payload(item_id="c1", body="changed"))
        self.assertEqual(code, product_cli.EXIT_UNAVAILABLE)
        self.assertEqual(payload["error"]["code"], intake.IntakeConflict.code)

        # The original is untouched; nothing was merged.
        code, shown = self._run(
            ["intake", "show", first["submission"]["submission_id"]]
        )
        self.assertEqual(shown["submission"]["body"], "original")
        _code, listing = self._run(["intake", "list"])
        self.assertEqual(listing["count"], 1)


class InjectionTests(_CliFixture):
    def test_shell_metacharacters_are_inert_data(self) -> None:
        marker = Path(self._temp.name) / "should-not-exist"
        body = (
            "; touch %s\n$(touch %s)\n`touch %s`\n| tee %s\n> %s\n"
            % (marker, marker, marker, marker, marker)
        )
        code, payload = self._pull(stdin=_payload(item_id="inj", body=body))
        self.assertEqual(code, product_cli.EXIT_OK)
        self.assertEqual(payload["status"], "ADMITTED")
        # Nothing was executed: the marker was never created.
        self.assertFalse(marker.exists())

        # The bytes are stored verbatim, as opaque content.
        _code, shown = self._run(
            ["intake", "show", payload["submission"]["submission_id"]]
        )
        self.assertEqual(shown["submission"]["body"], body)

    def test_bridge_module_imports_no_network_or_shell(self) -> None:
        import ast

        tree = ast.parse(Path(intake_bridge.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        # Standard library data-only imports plus this package, nothing else.
        self.assertEqual(imported - {"__future__", "collections", "typing", "intake"},
                         set())
        forbidden = {"socket", "http", "subprocess", "urllib", "ssl", "ctypes", "os"}
        self.assertEqual(imported & forbidden, set())


class FailClosedTransportTests(_CliFixture):
    def test_no_transport_is_reported(self) -> None:
        code, payload = self._pull(transport=None, stdin=_payload())
        self.assertEqual(code, product_cli.EXIT_UNAVAILABLE)
        self.assertEqual(payload["error"]["code"], "NO_INBOUND_TRANSPORT")

    def test_unsupported_transports_fail_closed_without_reading_or_writing(self) -> None:
        for transport in ("custom-mcp", "http-webhook", "marketplace.other"):
            with self.subTest(transport=transport):
                code, payload = self._pull(transport=transport, stdin=_payload())
                self.assertEqual(code, product_cli.EXIT_UNAVAILABLE)
                self.assertEqual(
                    payload["error"]["code"], intake_bridge.UnsupportedTransport.code
                )
        # A refused transport admitted nothing.
        _code, listing = self._run(["intake", "list"])
        self.assertEqual(listing["count"], 0)

    def test_deferred_github_transport_is_explicit(self) -> None:
        code, payload = self._pull(transport="github-issue", stdin=_payload())
        self.assertEqual(code, product_cli.EXIT_UNAVAILABLE)
        self.assertEqual(payload["error"]["code"], intake_bridge.DeferredTransport.code)

    def test_unsupported_transport_is_checked_before_the_stream_is_read(self) -> None:
        class _Exploding:
            def read(self, _size: int):  # pragma: no cover - must not be called
                raise AssertionError("stream must not be read for a bad transport")

        with self.assertRaises(intake_bridge.UnsupportedTransport):
            intake_bridge.admit_stream(_Exploding(), transport="custom-mcp")

    def test_cli_refuses_bad_transport_before_opening_the_file(self) -> None:
        missing = Path(self._temp.name) / "does-not-exist.json"
        for transport, code in (
            ("custom-mcp", intake_bridge.UnsupportedTransport.code),
            ("github-issue", intake_bridge.DeferredTransport.code),
        ):
            with self.subTest(transport=transport):
                _code, payload = self._run(
                    ["intake", "pull", "--transport", transport,
                     "--file", str(missing)]
                )
                # The transport is refused, not the missing file.
                self.assertEqual(payload["error"]["code"], code)

    def test_cli_refuses_bad_transport_before_consuming_stdin(self) -> None:
        class _Tripwire:
            def __init__(self) -> None:
                self.read_called = False

            def read(self, _size: int = -1) -> bytes:
                self.read_called = True
                return b""

        class _TripwireStdin:
            def __init__(self) -> None:
                self.buffer = _Tripwire()

        fake = _TripwireStdin()
        stream = io.StringIO()
        with mock.patch.dict(os.environ, self.env):
            with mock.patch("sys.stdin", fake):
                with contextlib.redirect_stdout(stream):
                    code = intake_cli.main(
                        ["pull", "--transport", "custom-mcp"]
                    )
        self.assertEqual(code, intake_cli.EXIT_UNAVAILABLE)
        self.assertFalse(fake.buffer.read_called)
        self.assertEqual(
            json.loads(stream.getvalue())["error"]["code"],
            intake_bridge.UnsupportedTransport.code,
        )


class BoundedInputTests(_CliFixture):
    def test_oversized_stdin_is_refused(self) -> None:
        blob = b"x" * (intake.MAX_SUBMISSION_BYTES + 1)
        code, payload = self._pull(stdin=blob)
        self.assertEqual(code, product_cli.EXIT_UNAVAILABLE)
        self.assertEqual(payload["error"]["code"], intake.IntakeInvalid.code)

    def test_duplicate_json_key_is_refused(self) -> None:
        raw = ('{"schema": "%s", "item_id": "a", "item_id": "b", "body": ""}'
               % intake.REQUEST_SCHEMA).encode("utf-8")
        code, payload = self._pull(stdin=raw)
        self.assertEqual(payload["error"]["code"], intake.IntakeInvalid.code)

    def test_non_utf8_is_refused(self) -> None:
        code, payload = self._pull(stdin=b"\xff\xfe\x00")
        self.assertEqual(payload["error"]["code"], intake.IntakeInvalid.code)

    def test_non_object_is_refused(self) -> None:
        _code, payload = self._pull(stdin=b"[1, 2, 3]")
        self.assertEqual(payload["error"]["code"], intake.IntakeInvalid.code)


class LibrarySurfaceTests(_CliFixture):
    def test_admit_document_reports_transport_and_durable_receipt(self) -> None:
        result = intake_bridge.admit_document(
            _doc(item_id="lib"), transport=TRANSPORT, env=self.env
        )
        self.assertEqual(result["bridge_schema"], intake_bridge.BRIDGE_SCHEMA)
        self.assertEqual(result["transport"], TRANSPORT)
        self.assertEqual(result["submission"]["item_id"], "lib")
        self.assertEqual(len(result["submission"]["submission_id"]), 64)
        self.assertTrue(result["submission"]["fingerprint"].startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
