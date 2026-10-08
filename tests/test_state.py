"""State I/O fails loudly on corrupt or unreadable state."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from cao.state import (
    StateCorruptError,
    StateError,
    StateSchemaError,
    StateUnreadableError,
    read_state,
    write_state,
)


class ReadStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_absent_file_returns_none(self) -> None:
        self.assertIsNone(read_state(self.dir / "missing.json"))

    def test_round_trip_object(self) -> None:
        path = self.dir / "state.json"
        path.write_text(json.dumps({"state": "ACTIVE", "n": 1}), encoding="utf-8")
        self.assertEqual(read_state(path), {"state": "ACTIVE", "n": 1})

    def test_corrupt_json_fails_loudly(self) -> None:
        path = self.dir / "state.json"
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(StateCorruptError) as ctx:
            read_state(path)
        self.assertEqual(ctx.exception.code, "STATE_CORRUPT")

    def test_empty_file_is_corrupt(self) -> None:
        path = self.dir / "state.json"
        path.write_text("", encoding="utf-8")
        with self.assertRaises(StateCorruptError):
            read_state(path)

    def test_non_object_json_fails_loudly(self) -> None:
        path = self.dir / "state.json"
        path.write_text(json.dumps([1, 2]), encoding="utf-8")
        with self.assertRaises(StateSchemaError) as ctx:
            read_state(path)
        self.assertEqual(ctx.exception.code, "STATE_SCHEMA")

    def test_scalar_json_fails_loudly(self) -> None:
        path = self.dir / "state.json"
        path.write_text("42", encoding="utf-8")
        with self.assertRaises(StateSchemaError):
            read_state(path)

    def test_directory_is_unreadable(self) -> None:
        with self.assertRaises(StateUnreadableError):
            read_state(self.dir)

    def test_permission_denied_fails_loudly(self) -> None:
        path = self.dir / "state.json"
        path.write_text(json.dumps({"a": 1}), encoding="utf-8")
        os.chmod(path, 0)
        try:
            try:
                read_state(path)
            except StateUnreadableError:
                return
            self.skipTest("process can read a 0o000 file (running privileged)")
        finally:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)

    def test_dangling_symlink_is_unreadable_not_absent(self) -> None:
        link = self.dir / "state.json"
        link.symlink_to(self.dir / "missing-target")
        with self.assertRaises(StateUnreadableError) as ctx:
            read_state(link)
        self.assertEqual(ctx.exception.code, "STATE_UNREADABLE")

    def test_invalid_utf8_is_corrupt(self) -> None:
        path = self.dir / "state.json"
        path.write_bytes(b"\xff\xfe")
        with self.assertRaises(StateCorruptError) as ctx:
            read_state(path)
        self.assertEqual(ctx.exception.code, "STATE_CORRUPT")

    def test_nonfinite_json_constants_are_corrupt(self) -> None:
        for token in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(token=token):
                path = self.dir / f"{token}.json"
                path.write_text('{"value": ' + token + "}", encoding="utf-8")
                with self.assertRaises(StateCorruptError):
                    read_state(path)

    def test_errors_are_state_errors(self) -> None:
        for exc in (
            StateCorruptError("x"),
            StateSchemaError("x"),
            StateUnreadableError("x"),
        ):
            self.assertIsInstance(exc, StateError)


class WriteStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_write_then_read_round_trips(self) -> None:
        path = self.dir / "state.json"
        result = write_state(path, {"state": "WAITING_EXTERNAL", "n": 3})
        self.assertEqual(result, {"state": "WAITING_EXTERNAL", "n": 3})
        self.assertEqual(read_state(path), result)

    def test_write_is_atomic_and_leaves_no_temp_files(self) -> None:
        path = self.dir / "state.json"
        write_state(path, {"a": 1})
        write_state(path, {"a": 2})
        leftovers = [p.name for p in self.dir.iterdir() if p.name != "state.json"]
        self.assertEqual(leftovers, [])
        self.assertEqual(read_state(path), {"a": 2})

    def test_write_rejects_non_serializable(self) -> None:
        path = self.dir / "state.json"
        with self.assertRaises(StateSchemaError):
            write_state(path, {"bad": object()})

    def test_write_rejects_non_mapping(self) -> None:
        path = self.dir / "state.json"
        with self.assertRaises(StateSchemaError):
            write_state(path, ["not", "a", "mapping"])  # type: ignore[arg-type]

    def test_write_to_missing_directory_fails_loudly(self) -> None:
        path = self.dir / "nested" / "state.json"
        with self.assertRaises(StateUnreadableError):
            write_state(path, {"a": 1})


class RejectedWriteTests(unittest.TestCase):
    """A rejected write must not replace prior durable bytes."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "state.json"

    def _preserved(self, rejected) -> None:
        write_state(self.path, {"previous": 1})
        before = self.path.read_bytes()
        with self.assertRaises(StateSchemaError):
            write_state(self.path, rejected)
        self.assertEqual(self.path.read_bytes(), before)
        leftovers = [p.name for p in self.dir.iterdir() if p.name != "state.json"]
        self.assertEqual(leftovers, [])

    def test_rejected_tuple_write_preserves_prior_bytes(self) -> None:
        self._preserved({"tuple": (1, 2)})

    def test_rejected_nonstring_key_write_preserves_prior_bytes(self) -> None:
        self._preserved({1: "one"})

    def test_rejected_nonfinite_value_preserves_prior_bytes(self) -> None:
        self._preserved({"value": float("nan")})

    def test_accepted_mapping_still_round_trips(self) -> None:
        result = write_state(self.path, {"nested": {"a": [1, 2, 3]}})
        self.assertEqual(result, {"nested": {"a": [1, 2, 3]}})
        self.assertEqual(read_state(self.path), result)


if __name__ == "__main__":
    unittest.main()
