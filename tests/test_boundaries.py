"""Boundary guards for the runtime-neutral CAO core and implementation lanes.

Concrete plugins are permitted only behind the runtime plugin boundary;
core dependencies, private data, and concern separation remain guarded.

The source traversal is RECURSIVE: the guards must apply to nested core
modules (for example ``cao/observers/``), not just the top-level package. The
one explicit exception is the ``cao/plugins/`` package, which is plugin-owned
and legitimately contains runtime-specific code, including the concrete runtime
name that core must never contain.
"""

from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
CAO_ROOT = SRC_ROOT / "cao"
DOCS_ROOT = REPO_ROOT / "docs"
PLUGINS_ROOT_NAME = "plugins"

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
    # Intake admission is a private inbound gate: it must not reach execution,
    # policy, the runtime contract or the Work Registry, so an admitted
    # submission can never start, merge or claim anything.
    "intake.py": (
        "runtime",
        "cao.runtime",
        "policy",
        "cao.policy",
        "contracts",
        "cao.contracts",
        "registry",
        "cao.registry",
    ),
    # The inbound bridge is the same private gate seen from the transport side:
    # it admits data and nothing else, so it must not reach execution, policy,
    # the runtime contract or the Work Registry either.
    "intake_bridge.py": (
        "runtime",
        "cao.runtime",
        "policy",
        "cao.policy",
        "contracts",
        "cao.contracts",
        "registry",
        "cao.registry",
    ),
}

PRIVATE_TOKENS = (
    "albr",
    "sentinelx",
    "/home/",
    "resident",
    "brain",
    "tolest",
    "companyos",
)


def _python_files(root: Path = CAO_ROOT) -> list[Path]:
    """Every Python source file below ``root``, recursively.

    Recursive on purpose: a top-level-only glob would silently skip every
    subpackage (``cao/observers/``, ``cao/plugins/``) and any future one.
    """

    return sorted(root.rglob("*.py"))


def _module_name(path: Path) -> str:
    """The importable dotted module name for ``path`` below ``src``."""

    relative = path.relative_to(SRC_ROOT)
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _is_plugin_file(path: Path, root: Path = CAO_ROOT) -> bool:
    """Whether ``path`` lives under ``root``'s plugins package.

    Deliberately narrow: only files under ``<root>/plugins/`` are plugin-owned,
    so a sibling such as ``<root>/plugins_extra.py`` is NOT exempt.
    """

    try:
        path.relative_to(root / PLUGINS_ROOT_NAME)
        return True
    except ValueError:
        return False


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _plugin_import_offenders(root: Path = CAO_ROOT) -> list[str]:
    """Core sources that import the concrete plugin package."""

    offenders: list[str] = []
    for path in _python_files(root):
        for module in _imports(path):
            if module in {"plugins", "cao.plugins"} or module.startswith(
                ("plugins.", "cao.plugins.")
            ):
                offenders.append(f"{path.relative_to(root)}: {module}")
    return offenders


def _herdr_offenders(root: Path = CAO_ROOT) -> list[str]:
    """Non-plugin sources that mention the concrete runtime name.

    Plugin-owned files are exempt: a runtime integration is expected to name
    its own runtime.
    """

    offenders: list[str] = []
    for path in _python_files(root):
        if _is_plugin_file(path, root):
            continue
        if "herdr" in path.read_text(encoding="utf-8").lower():
            offenders.append(str(path.relative_to(root)))
    return offenders


class ScopeTests(unittest.TestCase):
    def test_core_does_not_import_concrete_plugins(self) -> None:
        self.assertEqual(_plugin_import_offenders(), [])

    def test_no_herdr_in_source(self) -> None:
        self.assertEqual(_herdr_offenders(), [])


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
                    extra.append(f"{path.relative_to(CAO_ROOT)}: {module}")
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
            importlib.import_module(_module_name(path))


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


class RecursiveGuardTests(unittest.TestCase):
    """Proof that the guards genuinely cover nested core modules.

    Each case builds an ephemeral tree so a newly created nested module is
    detected without leaving anything behind. The tests fail if the traversal
    regresses to top-level-only, or if the plugin exception widens beyond the
    plugins package.
    """

    def _tree(self, layout: dict[str, str]) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / "cao"
        for relative, text in layout.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return root

    def test_nested_core_module_is_detected(self) -> None:
        root = self._tree(
            {
                "nested/__init__.py": "",
                "nested/offender.py": "RUNTIME = 'herdr'\n",
            }
        )
        self.assertIn("nested/offender.py", _herdr_offenders(root))
        self.assertIn(
            "nested/offender.py", [str(p.relative_to(root)) for p in _python_files(root)]
        )

    def test_nested_core_plugin_import_is_detected(self) -> None:
        root = self._tree(
            {
                "nested/__init__.py": "",
                "nested/offender.py": "import cao.plugins.herdr\n",
            }
        )
        offenders = _plugin_import_offenders(root)
        self.assertEqual(len(offenders), 1)
        self.assertIn("nested/offender.py", offenders[0])

    def test_plugins_package_is_exempt_but_only_plugins(self) -> None:
        root = self._tree(
            {
                "plugins/__init__.py": "",
                "plugins/runtime_x/__init__.py": "RUNTIME = 'herdr'\n",
                "observers/__init__.py": "",
                "observers/nested_offender.py": "RUNTIME = 'herdr'\n",
                "plugins_extra.py": "RUNTIME = 'herdr'\n",
            }
        )
        offenders = _herdr_offenders(root)
        self.assertIn("observers/nested_offender.py", offenders)
        self.assertIn("plugins_extra.py", offenders)
        self.assertNotIn("plugins/runtime_x/__init__.py", offenders)



class PublicOssCompatibilityTests(unittest.TestCase):
    """Regression checks for public interfaces kept within the governed scope."""

    def test_engineering_suite_identity_accepts_legacy_family_only(self) -> None:
        from cao import engineering_loop as engineering

        baseline, candidate, suite = "a" * 40, "b" * 40, "c" * 64
        manifest = {"suite_sha256": suite, "min_gain": 0.1}
        report = {
            "schema": engineering.METRIC_SCHEMA,
            "status": "SOURCE_REPORTED_IMPROVEMENT",
            "baseline_sha": baseline,
            "candidate_sha": candidate,
            "suite_sha256": suite,
            "objective_gain": 0.25,
            "source_only": True,
            "canonical_operating_proof": False,
        }
        self.assertEqual(engineering._comparison(report, manifest), (baseline, candidate))
        legacy = dict(report, schema="example.eval.comparison.v1")
        self.assertEqual(engineering._comparison(legacy, manifest), (baseline, candidate))
        for schema in ("cao.engineering.evaluation.v2", "other.report.v1",
                       "CAO.ENGINEERING.EVALUATION.V1", "..eval.comparison.v1"):
            with self.subTest(schema=schema), self.assertRaises(engineering.EngineeringError):
                engineering._comparison(dict(report, schema=schema), manifest)
        with self.assertRaises(engineering.EngineeringError):
            engineering._comparison(dict(report, suite_sha256="d" * 64), manifest)

    def test_installed_console_entrypoint_is_product_cli(self) -> None:
        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('cao = "cao.product_cli:main"', text)

    def test_foundation_commands_remain_reachable_through_product_cli(self) -> None:
        import contextlib
        import io
        import json
        import os
        from unittest import mock
        from cao import product_cli

        with tempfile.TemporaryDirectory() as directory:
            env = {"XDG_CONFIG_HOME": str(Path(directory) / "config"),
                   "XDG_STATE_HOME": str(Path(directory) / "state")}
            with mock.patch.dict(os.environ, env):
                with contextlib.redirect_stdout(out := io.StringIO()):
                    code = product_cli.main(["plugins", "list", "--json"])
                self.assertEqual(code, product_cli.EXIT_OK)
                self.assertEqual(json.loads(out.getvalue())["schema"],
                                 "cao.cli.plugins.v0")

                with contextlib.redirect_stdout(out := io.StringIO()):
                    code = product_cli.main(["run", "list"])
                self.assertEqual(code, product_cli.EXIT_UNAVAILABLE)
                listing = json.loads(out.getvalue())
                self.assertEqual(listing["schema"], "cao.cli.unavailable.v0")
                self.assertEqual(listing["command"], "list")

                with contextlib.redirect_stdout(out := io.StringIO()):
                    code = product_cli.main(["run"])
                self.assertEqual(code, product_cli.EXIT_UNAVAILABLE)
                task = json.loads(out.getvalue())
                self.assertEqual(task["schema"], "cao.product.unavailable.v0")
                self.assertEqual(task["command"], "run")
                self.assertEqual(task["error"]["code"], "NOT_IMPLEMENTED")


if __name__ == "__main__":
    unittest.main()
