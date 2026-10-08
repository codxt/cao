"""Boundary guards for the runtime-neutral CAO core and implementation lanes.

Concrete plugins are permitted only behind the runtime plugin boundary;
core dependencies, private data, and concern separation remain guarded.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
CAO_ROOT = SRC_ROOT / "cao"
DOCS_ROOT = REPO_ROOT / "docs"

# Concerns that must not leak into one another.
SEPARATION_RULES = {
    "policy.py": ("runtime", "cao.runtime", "contracts", "cao.contracts"),
    "runtime.py": ("policy", "cao.policy"),
    "state.py": (
        "runtime",
        "cao.runtime",
        "policy",
        "cao.policy",
        "contracts",
        "cao.contracts",
    ),
    "contracts.py": ("runtime", "cao.runtime", "policy", "cao.policy", "state", "cao.state"),
}

PRIVATE_TOKENS = ("albr", "sentinelx", "/home/", "resident")


def _python_files() -> list[Path]:
    return sorted(CAO_ROOT.glob("*.py"))


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class ScopeTests(unittest.TestCase):
    def test_core_does_not_import_concrete_plugins(self) -> None:
        offenders = []
        for path in _python_files():
            for module in _imports(path):
                if module in {"plugins", "cao.plugins"} or module.startswith(
                    ("plugins.", "cao.plugins.")
                ):
                    offenders.append(f"{path.name}: {module}")
        self.assertEqual(offenders, [])

    def test_no_herdr_in_source(self) -> None:
        offenders = []
        for path in _python_files():
            if "herdr" in path.read_text(encoding="utf-8").lower():
                offenders.append(path.name)
        self.assertEqual(offenders, [])


class DependencyBoundaryTests(unittest.TestCase):
    def test_core_uses_only_stdlib_and_cao(self) -> None:
        extra: list[str] = []
        for path in _python_files():
            for module in _imports(path):
                top = module.split(".")[0]
                if top == "cao":
                    continue
                if (CAO_ROOT / f"{top}.py").exists():
                    # A sibling module in this package (relative import).
                    continue
                if top not in sys.stdlib_module_names:
                    extra.append(f"{path.name}: {module}")
        self.assertEqual(extra, [])

    def test_concern_separation(self) -> None:
        violations: list[str] = []
        for filename, forbidden in SEPARATION_RULES.items():
            imports = _imports(CAO_ROOT / filename)
            for module in forbidden:
                if module in imports:
                    violations.append(f"{filename} imports {module}")
        self.assertEqual(violations, [])


class PrivacyTests(unittest.TestCase):
    def test_no_private_tokens_in_source_or_docs(self) -> None:
        offenders: list[str] = []
        targets = sorted(CAO_ROOT.rglob("*.py"))
        if DOCS_ROOT.exists():
            targets.extend(sorted(DOCS_ROOT.glob("*.md")))
        for path in targets:
            text = path.read_text(encoding="utf-8").lower()
            for token in PRIVATE_TOKENS:
                if token in text:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {token}")
        self.assertEqual(offenders, [])


class ImportabilityTests(unittest.TestCase):
    def test_all_core_modules_import(self) -> None:
        import importlib

        for path in _python_files():
            if path.name == "__main__.py":
                continue
            importlib.import_module(f"cao.{path.stem}")


class ContractBoundaryTests(unittest.TestCase):
    def test_capability_values_are_unique_and_complete(self) -> None:
        from cao.contracts import Capability

        values = [cap.value for cap in Capability]
        self.assertEqual(len(values), len(set(values)))
        self.assertEqual(
            set(values),
            {
                "capability.discovery",
                "snapshot.capture",
                "target.inspect",
                "output.observe",
                "execution.start",
                "session.prompt",
                "session.close",
            },
        )

    def test_default_registry_ships_no_runtime(self) -> None:
        from cao.runtime import PluginRegistry

        # An environment with no installed plugins yields an empty registry;
        # CAO never ships a built-in runtime.
        self.assertEqual(PluginRegistry.discover(entry_points=[]).ids(), ())


if __name__ == "__main__":
    unittest.main()
