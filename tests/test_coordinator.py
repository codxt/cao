"""Reconciler tests: unknown, disagreement and refusal paths are first-class.

Also holds the lane self-enforcement guard. The existing boundary suite only
globs ``src/cao/*.py`` at the top level, so it never inspects
``src/cao/observers``. These tests self-enforce the same rules over every file
this lane owns.
"""

from __future__ import annotations

import ast
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cao.coordinator import (
    CoordinatorBoundError,
    CoordinatorError,
    IntendedState,
    ObservedState,
    ProbeUnavailable,
    ReconcileVerdict,
    reconcile,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Every path this lane owns. The hygiene tests scan exactly these.
OWNED_FILES = (
    REPO_ROOT / "src" / "cao" / "coordinator.py",
    REPO_ROOT / "src" / "cao" / "portfolio.py",
    REPO_ROOT / "src" / "cao" / "observers" / "__init__.py",
    REPO_ROOT / "src" / "cao" / "observers" / "github.py",
    REPO_ROOT / "tests" / "test_coordinator.py",
    REPO_ROOT / "tests" / "test_portfolio.py",
    REPO_ROOT / "tests" / "test_observers_github.py",
)

# Split so this guard file does not itself contain the tokens it forbids.
FORBIDDEN_TOKENS = ("alb" + "r", "sentinel" + "x", "/hom" + "e/", "resid" + "ent")
FORBIDDEN_PLUGIN = "her" + "dr"

# Import roots that would mean real I/O, a third-party dependency, or a
# cross-lane reach. The generic core must import none of them.
FORBIDDEN_IMPORTS = (
    "requests",
    "httpx",
    "urllib",
    "socket",
    "http",
    "subprocess",
    "cao.lifecycle",
    "cao.launcher",
    "cao.plugins",
    "lifecycle",
    "launcher",
    "plugins",
)

# Package roots used to resolve relative imports for the direction guard. The
# resolver must turn `from ..evidence import X` in cao/observers/github.py into
# `cao.evidence`, and `from .evidence import X` in cao/coordinator.py into
# `cao.evidence`, rather than skipping relative imports entirely.
_PACKAGE_ROOTS = (REPO_ROOT / "src", REPO_ROOT / "tests")


def _module_package(path: Path) -> tuple[str, str]:
    """Return ``(module, package)`` for a source file in this repository."""

    resolved = path.resolve()
    for root in _PACKAGE_ROOTS:
        try:
            rel = resolved.relative_to(root)
            break
        except ValueError:
            continue
    else:
        rel = resolved.relative_to(REPO_ROOT)
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        module = ".".join(parts[:-1])
        return module, module
    return ".".join(parts), ".".join(parts[:-1])


def _anchor(package: str, level: int) -> str | None:
    """Resolve the base package for a relative import of ``level`` dots.

    A level-1 import anchors at the importing module's own package; each extra
    dot walks one package up. ``None`` means the import walks above the top
    level, which Python itself rejects.
    """

    parts = package.split(".") if package else []
    drop = level - 1
    # Dropping exactly the package's own segments would leave nothing to
    # import from, which is the beyond-top-level case Python rejects.
    if drop >= len(parts):
        return None
    if drop == 0:
        return package
    return ".".join(parts[: len(parts) - drop])


def _referenced_modules(source: str, package: str) -> list[str]:
    """Every fully-qualified module a source text references.

    Relative imports are resolved against ``package`` instead of being skipped,
    so the direction guard sees the module they actually reach. Imported names
    are also considered as candidate submodules, which is how
    ``from .. import launcher`` is caught.
    """

    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = _anchor(package, node.level)
                if anchor is None:
                    continue
                base = f"{anchor}.{node.module}" if node.module else anchor
            else:
                base = node.module or ""
            found.append(base)
            found.extend(f"{base}.{alias.name}" for alias in node.names)
    return found


def _is_forbidden(module: str) -> bool:
    return module in FORBIDDEN_IMPORTS or any(
        module.startswith(bad + ".") for bad in FORBIDDEN_IMPORTS
    )


def _stdlib_violations(
    paths: tuple[Path, ...], sources: dict[Path, str] | None = None
) -> list[str]:
    extra: list[str] = []
    for path in paths:
        source = (sources or {}).get(path)
        if source is None:
            source = path.read_text(encoding="utf-8")
        _, package = _module_package(path)
        for module in _referenced_modules(source, package):
            top = module.split(".")[0]
            if top in ("cao", "__future__"):
                continue
            if top not in sys.stdlib_module_names:
                extra.append(f"{path.name}: {module}")
    return extra


def _direction_violations(
    paths: tuple[Path, ...], sources: dict[Path, str] | None = None
) -> list[str]:
    offenders: list[str] = []
    for path in paths:
        source = (sources or {}).get(path)
        if source is None:
            source = path.read_text(encoding="utf-8")
        _, package = _module_package(path)
        for module in _referenced_modules(source, package):
            if _is_forbidden(module):
                offenders.append(f"{path.name}: {module}")
    return offenders


def fixed_clock():
    return lambda: datetime(2020, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


class ReconcileTests(unittest.TestCase):
    def test_agreement(self) -> None:
        outcome = reconcile(
            [IntendedState("lane-a", "RUNNING")],
            lambda subject: ObservedState(subject, "RUNNING"),
            clock=fixed_clock(),
        )
        result = outcome.subjects[0]
        self.assertIs(result.verdict, ReconcileVerdict.AGREEMENT)
        self.assertTrue(result.evidence.supported)
        self.assertEqual(result.intended_state, "RUNNING")
        self.assertEqual(result.observed_state, "RUNNING")
        self.assertEqual(result.evidence.capture_time, "2020-01-01T00:00:00Z")

    def test_disagreement_is_reported_not_resolved(self) -> None:
        outcome = reconcile(
            [IntendedState("lane-a", "RUNNING")],
            lambda subject: ObservedState(subject, "STOPPED"),
        )
        result = outcome.subjects[0]
        self.assertIs(result.verdict, ReconcileVerdict.DISAGREEMENT)
        # Both sides are preserved; neither is preferred or overwritten.
        self.assertEqual(result.intended_state, "RUNNING")
        self.assertEqual(result.observed_state, "STOPPED")
        self.assertTrue(result.evidence.supported)
        self.assertEqual(outcome.verdict_counts()["DISAGREEMENT"], 1)

    def test_never_observed_is_unknown_not_absent(self) -> None:
        def probe(subject):
            return None

        result = reconcile(
            [IntendedState("lane-a", "RUNNING")], probe
        ).subjects[0]
        self.assertIs(result.verdict, ReconcileVerdict.UNKNOWN)
        self.assertIsNone(result.observed_state)
        self.assertFalse(result.evidence.supported)
        reason = (result.evidence.reason or "").lower()
        self.assertNotIn("absent", reason)
        self.assertNotIn("missing", reason)
        self.assertNotIn("gone", reason)

    def test_probe_unavailable_is_unknown(self) -> None:
        def probe(subject):
            raise ProbeUnavailable("probe endpoint offline")

        result = reconcile(
            [IntendedState("lane-a", "RUNNING")], probe
        ).subjects[0]
        self.assertIs(result.verdict, ReconcileVerdict.UNKNOWN)
        self.assertIsNone(result.observed_state)
        self.assertEqual(result.evidence.verdict.value, "UNAVAILABLE")

    def test_probe_exception_is_unknown_and_not_propagated(self) -> None:
        def probe(subject):
            raise RuntimeError("boom")

        result = reconcile(
            [IntendedState("lane-a", "RUNNING")], probe
        ).subjects[0]
        self.assertIs(result.verdict, ReconcileVerdict.UNKNOWN)
        self.assertEqual(result.evidence.verdict.value, "UNKNOWN")
        self.assertNotIn("boom", result.evidence.reason or "")

    def test_malformed_probe_result_is_unknown(self) -> None:
        for hostile in ("RUNNING", 42, {"state": "RUNNING"}, ["RUNNING"]):
            with self.subTest(hostile=hostile):
                result = reconcile(
                    [IntendedState("lane-a", "RUNNING")], lambda subject: hostile
                ).subjects[0]
                self.assertIs(result.verdict, ReconcileVerdict.UNKNOWN)
                self.assertEqual(result.evidence.verdict.value, "INVALID")

    def test_observation_for_a_different_subject_is_unknown(self) -> None:
        result = reconcile(
            [IntendedState("lane-a", "RUNNING")],
            lambda subject: ObservedState("lane-b", "RUNNING"),
        ).subjects[0]
        self.assertIs(result.verdict, ReconcileVerdict.UNKNOWN)
        self.assertEqual(result.evidence.verdict.value, "INVALID")

    def test_contradictory_intended_is_unknown_and_probe_is_skipped(self) -> None:
        calls: list[str] = []

        def probe(subject):
            calls.append(subject)
            return ObservedState(subject, "RUNNING")

        outcome = reconcile(
            [
                IntendedState("lane-a", "RUNNING"),
                IntendedState("lane-a", "STOPPED"),
            ],
            probe,
        )
        result = outcome.subjects[0]
        self.assertIs(result.verdict, ReconcileVerdict.UNKNOWN)
        self.assertIsNone(result.intended_state)
        self.assertEqual(result.evidence.verdict.value, "INVALID")
        self.assertEqual(calls, [])

    def test_identical_duplicate_intended_is_deduped(self) -> None:
        outcome = reconcile(
            [
                IntendedState("lane-a", "RUNNING"),
                IntendedState("lane-a", "RUNNING"),
            ],
            lambda subject: ObservedState(subject, "RUNNING"),
        )
        self.assertEqual(len(outcome.subjects), 1)

    def test_bound_exceeded_raises_before_any_probe(self) -> None:
        calls: list[str] = []

        def probe(subject):
            calls.append(subject)
            return ObservedState(subject, "RUNNING")

        with self.assertRaises(CoordinatorBoundError):
            reconcile(
                [IntendedState(f"lane-{i}", "RUNNING") for i in range(3)],
                probe,
                max_subjects=2,
            )
        self.assertEqual(calls, [])

    def test_non_callable_probe_raises(self) -> None:
        with self.assertRaises(CoordinatorError):
            reconcile([IntendedState("lane-a", "RUNNING")], "not-callable")

    def test_empty_intended_yields_no_subjects(self) -> None:
        outcome = reconcile([], lambda subject: None)
        self.assertEqual(outcome.subjects, ())


class LaneHygieneTests(unittest.TestCase):
    """Self-enforcement for the subpackage the top-level guard cannot see."""

    def test_owned_paths_exist(self) -> None:
        missing = [str(path) for path in OWNED_FILES if not path.exists()]
        self.assertEqual(missing, [])
        self.assertEqual(len(set(OWNED_FILES)), 7)

    def test_no_private_or_plugin_tokens(self) -> None:
        offenders: list[str] = []
        for path in OWNED_FILES:
            text = path.read_text(encoding="utf-8").lower()
            for token in FORBIDDEN_TOKENS:
                if token in text:
                    offenders.append(f"{path.name}: private token")
            if FORBIDDEN_PLUGIN in text:
                offenders.append(f"{path.name}: plugin name")
        self.assertEqual(offenders, [])

    def test_imports_resolve_to_stdlib_or_cao(self) -> None:
        self.assertEqual(_stdlib_violations(OWNED_FILES), [])

    def test_no_network_or_process_imports(self) -> None:
        self.assertEqual(_direction_violations(OWNED_FILES), [])

    def test_relative_import_resolution_matches_the_layout(self) -> None:
        coordinator = REPO_ROOT / "src" / "cao" / "coordinator.py"
        github = REPO_ROOT / "src" / "cao" / "observers" / "github.py"
        self.assertEqual(_module_package(coordinator)[1], "cao")
        self.assertEqual(_module_package(github)[1], "cao.observers")
        self.assertEqual(_anchor("cao", 1), "cao")
        self.assertEqual(_anchor("cao.observers", 2), "cao")
        self.assertEqual(_anchor("cao.observers", 3), None)
        self.assertIn(
            "cao.evidence",
            _referenced_modules("from .evidence import Evidence", "cao"),
        )
        self.assertIn(
            "cao.evidence",
            _referenced_modules("from ..evidence import Evidence", "cao.observers"),
        )

    def test_injected_relative_lifecycle_import_fails_direction_guard(self) -> None:
        github = REPO_ROOT / "src" / "cao" / "observers" / "github.py"
        source = github.read_text(encoding="utf-8") + (
            "\nfrom ..lifecycle import WorkerState\n"
        )
        offenders = _direction_violations(OWNED_FILES, {github: source})
        self.assertTrue(any("lifecycle" in item for item in offenders), offenders)

    def test_injected_relative_launcher_import_fails_direction_guard(self) -> None:
        github = REPO_ROOT / "src" / "cao" / "observers" / "github.py"
        source = github.read_text(encoding="utf-8") + "\nfrom .. import launcher\n"
        offenders = _direction_violations(OWNED_FILES, {github: source})
        self.assertTrue(any("launcher" in item for item in offenders), offenders)

    def test_injected_relative_plugins_import_fails_direction_guard(self) -> None:
        github = REPO_ROOT / "src" / "cao" / "observers" / "github.py"
        # Built from the split constant so this guard file does not itself
        # contain the plugin word it forbids.
        injected = "\nfrom ..plugins." + FORBIDDEN_PLUGIN + " import x\n"
        source = github.read_text(encoding="utf-8") + injected
        offenders = _direction_violations(OWNED_FILES, {github: source})
        # Caught by the DIRECTION rule, independent of the incidental token grep.
        self.assertTrue(any("plugins" in item for item in offenders), offenders)

    def test_legitimate_relative_imports_pass_both_guards(self) -> None:
        github = REPO_ROOT / "src" / "cao" / "observers" / "github.py"
        coordinator = REPO_ROOT / "src" / "cao" / "coordinator.py"
        sources = {
            github: github.read_text(encoding="utf-8")
            + "\nfrom ..evidence import Evidence\n",
            coordinator: coordinator.read_text(encoding="utf-8")
            + "\nfrom .evidence import Evidence\n",
        }
        self.assertEqual(_direction_violations(OWNED_FILES, sources), [])
        self.assertEqual(_stdlib_violations(OWNED_FILES, sources), [])


    def test_no_implicit_environment_credential(self) -> None:
        github = (REPO_ROOT / "src" / "cao" / "observers" / "github.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("os.environ", github)
        self.assertNotIn("getenv", github)
        self.assertNotIn("putenv", github)


if __name__ == "__main__":
    unittest.main()
