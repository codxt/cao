"""Tests for the worker execution lifecycle state machine."""

from __future__ import annotations

import ast
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cao import lifecycle as lifecycle_module
from cao.evidence import Evidence, EvidenceSource, Verdict, build_evidence
from cao.lifecycle import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    IllegalTransition,
    Lifecycle,
    LifecycleError,
    WorkerState,
    is_positive_evidence,
)

FIXED = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


class SequenceClock:
    """A deterministic clock: returns each supplied moment once."""

    def __init__(self, *moments: datetime) -> None:
        self._moments = list(moments)
        self._index = 0

    def __call__(self) -> datetime:
        index = min(self._index, len(self._moments) - 1)
        self._index += 1
        return self._moments[index]


def positive_evidence() -> object:
    return build_evidence(
        EvidenceSource.INTERNAL,
        Verdict.SUPPORTED,
        fingerprint_value="sha256:" + "0" * 64,
        capture_time="2026-01-01T00:00:00Z",
    )


class InitialisationTests(unittest.TestCase):
    def test_initial_state_and_history(self) -> None:
        machine = Lifecycle(WorkerState.PENDING, clock=lambda: FIXED)
        self.assertIs(machine.state, WorkerState.PENDING)
        self.assertFalse(machine.is_terminal)
        self.assertEqual(len(machine.history), 1)
        first = machine.history[0]
        self.assertIsNone(first.from_state)
        self.assertIs(first.to_state, WorkerState.PENDING)
        self.assertEqual(first.at, "2026-01-01T00:00:00Z")

    def test_invalid_initial_state_raises(self) -> None:
        with self.assertRaises(LifecycleError):
            Lifecycle("pending")  # type: ignore[arg-type]


class TransitionTests(unittest.TestCase):
    def test_happy_path_reaches_terminal_completed(self) -> None:
        machine = Lifecycle(clock=lambda: FIXED)
        machine.advance(WorkerState.STARTING)
        machine.advance(WorkerState.RUNNING)
        machine.advance(WorkerState.COMPLETED)
        self.assertIs(machine.state, WorkerState.COMPLETED)
        self.assertTrue(machine.is_terminal)
        self.assertEqual(len(machine.history), 4)

    def test_illegal_transition_raises_and_leaves_state(self) -> None:
        machine = Lifecycle(clock=lambda: FIXED)
        with self.assertRaises(IllegalTransition):
            machine.advance(WorkerState.RUNNING)  # pending cannot jump to running
        self.assertIs(machine.state, WorkerState.PENDING)
        self.assertEqual(len(machine.history), 1)

    def test_non_state_target_raises(self) -> None:
        machine = Lifecycle(clock=lambda: FIXED)
        with self.assertRaises(IllegalTransition):
            machine.advance("running")  # type: ignore[arg-type]
        self.assertIs(machine.state, WorkerState.PENDING)

    def test_terminal_states_are_terminal(self) -> None:
        for terminal in TERMINAL_STATES:
            machine = Lifecycle(terminal, clock=lambda: FIXED)
            for target in (WorkerState.STARTING, terminal):
                with self.assertRaises(IllegalTransition):
                    machine.advance(target)
            self.assertIs(machine.state, terminal)

    def test_unknown_is_terminal(self) -> None:
        machine = Lifecycle(WorkerState.UNKNOWN, clock=lambda: FIXED)
        self.assertTrue(machine.is_terminal)
        with self.assertRaises(IllegalTransition):
            machine.advance(WorkerState.RUNNING)

    def test_transition_table_is_total_and_explicit(self) -> None:
        self.assertEqual(set(ALLOWED_TRANSITIONS), set(WorkerState))
        for state in TERMINAL_STATES:
            self.assertEqual(ALLOWED_TRANSITIONS[state], frozenset())

    def test_no_state_is_its_own_successor(self) -> None:
        for state, targets in ALLOWED_TRANSITIONS.items():
            self.assertNotIn(state, targets)

    def test_transition_graph_is_acyclic_and_bounded(self) -> None:
        def longest(state: WorkerState, seen: frozenset) -> int:
            if state in TERMINAL_STATES:
                return 0
            best = 0
            for nxt in ALLOWED_TRANSITIONS[state]:
                if nxt in seen:
                    raise AssertionError(f"cycle detected through {nxt.value}")
                best = max(best, 1 + longest(nxt, seen | {state}))
            return best

        for state in WorkerState:
            self.assertLessEqual(longest(state, frozenset()), len(WorkerState))


class EvidenceResolutionTests(unittest.TestCase):
    def test_positive_evidence_advances_to_intended(self) -> None:
        machine = Lifecycle(clock=lambda: FIXED)
        machine.advance(WorkerState.STARTING)
        machine.resolve(positive_evidence(), WorkerState.RUNNING)
        self.assertIs(machine.state, WorkerState.RUNNING)

    def test_absent_evidence_resolves_unknown(self) -> None:
        machine = Lifecycle(clock=lambda: FIXED)
        machine.advance(WorkerState.STARTING)
        machine.resolve(None, WorkerState.RUNNING, reason="no observation")
        self.assertIs(machine.state, WorkerState.UNKNOWN)
        self.assertIn("no observation", machine.history[-1].reason or "")

    def test_non_supported_verdicts_resolve_unknown(self) -> None:
        for verdict in (
            Verdict.UNKNOWN,
            Verdict.UNAVAILABLE,
            Verdict.REFUSED,
            Verdict.INVALID,
        ):
            machine = Lifecycle(clock=lambda: FIXED)
            machine.advance(WorkerState.STARTING)
            evidence = build_evidence(
                EvidenceSource.RUNTIME,
                verdict,
                capture_time="2026-01-01T00:00:00Z",
                fingerprint_value="sha256:" + "1" * 64,
            )
            machine.resolve(evidence, WorkerState.RUNNING)
            self.assertIs(machine.state, WorkerState.UNKNOWN, verdict)

    def test_incomplete_supported_evidence_resolves_unknown(self) -> None:
        incomplete = (
            Evidence(
                EvidenceSource.INTERNAL,
                Verdict.SUPPORTED,
                capture_time="2026-01-01T00:00:00Z",
                fingerprint=None,
            ),
            Evidence(
                EvidenceSource.INTERNAL,
                Verdict.SUPPORTED,
                capture_time=None,
                fingerprint="sha256:" + "2" * 64,
            ),
        )
        for evidence in incomplete:
            self.assertFalse(is_positive_evidence(evidence))
            machine = Lifecycle(clock=lambda: FIXED)
            machine.advance(WorkerState.STARTING)
            machine.resolve(evidence, WorkerState.RUNNING)
            self.assertIs(machine.state, WorkerState.UNKNOWN)

    def test_non_evidence_object_resolves_unknown(self) -> None:
        machine = Lifecycle(clock=lambda: FIXED)
        machine.advance(WorkerState.STARTING)
        machine.resolve({"verdict": "SUPPORTED"}, WorkerState.RUNNING)
        self.assertIs(machine.state, WorkerState.UNKNOWN)

    def test_resolve_from_unknown_is_illegal(self) -> None:
        machine = Lifecycle(clock=lambda: FIXED)
        machine.advance(WorkerState.STARTING)
        machine.resolve(None, WorkerState.RUNNING)
        with self.assertRaises(IllegalTransition):
            machine.resolve(positive_evidence(), WorkerState.RUNNING)

    def test_invalid_intended_state_raises(self) -> None:
        machine = Lifecycle(clock=lambda: FIXED)
        machine.advance(WorkerState.STARTING)
        with self.assertRaises(LifecycleError):
            machine.resolve(positive_evidence(), "running")  # type: ignore[arg-type]


class ClockTests(unittest.TestCase):
    def test_transitions_use_the_injected_clock(self) -> None:
        moments = [
            datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc),
            datetime(2026, 3, 4, 5, 6, 8, tzinfo=timezone.utc),
            datetime(2026, 3, 4, 5, 6, 9, tzinfo=timezone.utc),
        ]
        machine = Lifecycle(clock=SequenceClock(*moments))
        machine.advance(WorkerState.STARTING)
        machine.advance(WorkerState.RUNNING)
        self.assertEqual(
            [entry.at for entry in machine.history],
            [
                "2026-03-04T05:06:07Z",
                "2026-03-04T05:06:08Z",
                "2026-03-04T05:06:09Z",
            ],
        )


class ModuleHygieneTests(unittest.TestCase):
    """Static checks supporting the lane invariants."""

    def _imports(self) -> set[str]:
        tree = ast.parse(Path(lifecycle_module.__file__).read_text("utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    names.add("cao." + (node.module or ""))
                elif node.module:
                    names.add(node.module)
        return names

    def test_imports_are_stdlib_or_cao_only(self) -> None:
        for module in self._imports():
            top = module.split(".")[0]
            if top == "cao":
                continue
            self.assertIn(top, sys.stdlib_module_names, module)

    def test_does_not_import_concrete_plugins(self) -> None:
        for module in self._imports():
            self.assertFalse(
                module == "plugins"
                or module == "cao.plugins"
                or module.startswith(("plugins.", "cao.plugins.")),
                module,
            )

    def test_does_not_import_subprocess_or_time(self) -> None:
        self.assertNotIn("subprocess", self._imports())
        self.assertNotIn("time", self._imports())


if __name__ == "__main__":
    unittest.main()
