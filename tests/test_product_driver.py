"""Tests for the runtime CLI driver.

Every test injects a fake runner; nothing here touches a live runtime. The
suite pins argv construction (lists, never shell strings), refusal of unsafe
bare arguments before any call, response binding, and sanitized failures.
"""

from __future__ import annotations

import json
import unittest

from cao import product_driver
from cao.product_driver import RuntimeDriver


def envelope(result_type: str, **fields) -> str:
    payload = {"type": result_type}
    payload.update(fields)
    return json.dumps({"id": "cli:test", "result": payload})


class FakeRunner:
    """Map an exact argv tuple to stdout or a raised error."""

    def __init__(self, responses=None):
        self.responses = dict(responses or {})
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if tuple(argv) not in self.responses:
            raise AssertionError(f"unexpected argv {list(argv)!r}")
        value = self.responses[tuple(argv)]
        if isinstance(value, BaseException):
            raise value
        return value


class WorkspaceTests(unittest.TestCase):
    def test_workspace_create_builds_a_list_and_binds_ids(self) -> None:
        runner = FakeRunner(
            {
                (
                    "workspace",
                    "create",
                    "--label",
                    "cao-review-t1",
                    "--no-focus",
                    "--cwd",
                    "/tmp/repo",
                ): envelope(
                    "workspace_created",
                    workspace={"workspace_id": "w1"},
                    tab={"tab_id": "w1:t1"},
                    root_pane={"pane_id": "w1:p1"},
                )
            }
        )
        driver = RuntimeDriver(runner=runner)
        out = driver.workspace_create("cao-review-t1", cwd="/tmp/repo")
        self.assertEqual(
            out, {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1"}
        )
        self.assertIsInstance(runner.calls[0], list)
        self.assertNotIsInstance(runner.calls[0], str)

    def test_workspace_create_refuses_an_option_like_label_before_any_call(self) -> None:
        runner = FakeRunner({})
        driver = RuntimeDriver(runner=runner)
        with self.assertRaises(product_driver.DriverUsageError):
            driver.workspace_create("-evil")
        self.assertEqual(runner.calls, [])

    def test_workspace_list_parses_entries(self) -> None:
        runner = FakeRunner(
            {
                ("workspace", "list"): envelope(
                    "workspace_list",
                    workspaces=[
                        {"workspace_id": "w1", "label": "a", "active_tab_id": "w1:t1"},
                        {"workspace_id": "w2", "label": "b"},
                    ],
                )
            }
        )
        driver = RuntimeDriver(runner=runner)
        listed = driver.workspace_list()
        self.assertEqual([w["workspace_id"] for w in listed], ["w1", "w2"])

    def test_workspace_list_rejects_a_missing_array(self) -> None:
        runner = FakeRunner({("workspace", "list"): envelope("workspace_list")})
        driver = RuntimeDriver(runner=runner)
        with self.assertRaises(product_driver.DriverProtocolError):
            driver.workspace_list()


class PaneTests(unittest.TestCase):
    def test_pane_read_returns_plain_text(self) -> None:
        runner = FakeRunner(
            {
                ("pane", "read", "w1:p1", "--source", "recent", "--lines", "10"): "hello\n"
            }
        )
        driver = RuntimeDriver(runner=runner)
        self.assertEqual(driver.pane_read("w1:p1", lines=10), "hello\n")

    def test_pane_create_uses_split_and_binds_the_new_pane(self) -> None:
        runner = FakeRunner(
            {
                (
                    "pane",
                    "split",
                    "w1:p1",
                    "--direction",
                    "down",
                    "--no-focus",
                    "--cwd",
                    "/tmp/repo",
                ): envelope("pane_created", pane={"pane_id": "w1:p9"})
            }
        )
        driver = RuntimeDriver(runner=runner)
        self.assertEqual(
            driver.pane_create("w1:p1", direction="down", cwd="/tmp/repo"), "w1:p9"
        )

    def test_pane_create_rejects_an_unknown_direction(self) -> None:
        runner = FakeRunner({})
        driver = RuntimeDriver(runner=runner)
        with self.assertRaises(product_driver.DriverUsageError):
            driver.pane_create("w1:p1", direction="sideways")
        self.assertEqual(runner.calls, [])


class AgentTests(unittest.TestCase):
    def test_agent_start_passes_kind_and_trailing_arguments(self) -> None:
        runner = FakeRunner(
            {
                (
                    "agent",
                    "start",
                    "cao1",
                    "--kind",
                    "opencode",
                    "--pane",
                    "w1:p1",
                    "--timeout",
                    "1000",
                    "--",
                    "--model",
                    "m",
                ): envelope(
                    "agent_started", agent={"pane_id": "w1:p1", "workspace_id": "w1"}
                )
            }
        )
        driver = RuntimeDriver(runner=runner)
        out = driver.agent_start(
            "cao1",
            "opencode",
            "w1:p1",
            argv=("--model", "m"),
            timeout_ms=1000,
        )
        self.assertEqual(out["agent_pane_id"], "w1:p1")

    def test_agent_prompt_refuses_option_like_text_before_any_call(self) -> None:
        runner = FakeRunner({})
        driver = RuntimeDriver(runner=runner)
        with self.assertRaises(product_driver.DriverUsageError):
            driver.agent_prompt("cao1", "-rm -rf /")
        self.assertEqual(runner.calls, [])

    def test_agent_prompt_binds_the_acknowledgement(self) -> None:
        runner = FakeRunner(
            {
                ("agent", "prompt", "cao1", "Review this"): envelope(
                    "agent_prompted", agent={"pane_id": "w1:p1", "workspace_id": "w1"}
                )
            }
        )
        driver = RuntimeDriver(runner=runner)
        out = driver.agent_prompt("cao1", "Review this")
        self.assertEqual(out["agent_pane_id"], "w1:p1")

    def test_agent_wait_extracts_a_state(self) -> None:
        runner = FakeRunner(
            {
                ("agent", "wait", "cao1", "--until", "idle", "--until", "done"): envelope(
                    "agent_wait", state="done"
                )
            }
        )
        driver = RuntimeDriver(runner=runner)
        result = driver.agent_wait("cao1", until=("idle", "done"))
        self.assertEqual(product_driver.wait_state(result), "done")


class FailureTests(unittest.TestCase):
    def test_non_json_output_is_a_protocol_error(self) -> None:
        runner = FakeRunner({("workspace", "list"): "not json"})
        driver = RuntimeDriver(runner=runner)
        with self.assertRaises(product_driver.DriverProtocolError):
            driver.workspace_list()

    def test_unrecognized_error_code_is_not_echoed(self) -> None:
        body = json.dumps(
            {"id": "x", "error": {"code": "internal_secret_code_xyz"}}
        )
        runner = FakeRunner({("workspace", "list"): body})
        driver = RuntimeDriver(runner=runner)
        with self.assertRaises(product_driver.DriverProtocolError) as ctx:
            driver.workspace_list()
        self.assertNotIn("internal_secret_code_xyz", str(ctx.exception))

    def test_known_not_found_code_maps_to_not_found(self) -> None:
        body = json.dumps({"id": "x", "error": {"code": "workspace_not_found"}})
        runner = FakeRunner({("workspace", "list"): body})
        driver = RuntimeDriver(runner=runner)
        with self.assertRaises(product_driver.DriverNotFound):
            driver.workspace_list()

    def test_missing_executable_is_sanitized(self) -> None:
        driver = RuntimeDriver(executable="/nonexistent/no-such-runtime-binary")
        with self.assertRaises(product_driver.DriverTransportError) as ctx:
            driver.workspace_list()
        self.assertNotIn("/nonexistent", str(ctx.exception))

    def test_no_executable_configured_is_unavailable(self) -> None:
        with self.assertRaises(product_driver.DriverUnavailable):
            RuntimeDriver(executable=None)

    def test_wait_state_returns_none_when_absent(self) -> None:
        self.assertIsNone(product_driver.wait_state({"type": "agent_wait"}))


if __name__ == "__main__":
    unittest.main()
