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
    # B3 advisory triage reads existing context (including the Work Registry,
    # read-only) but must not reach execution, policy or the runtime contract,
    # so an advisory result can never start, merge or claim anything. Its
    # Registry access is separately restricted to read-only calls below.
    "intake_triage.py": (
        "runtime",
        "cao.runtime",
        "policy",
        "cao.policy",
        "contracts",
        "cao.contracts",
    ),
    # B4 routing reads existing context (including the Work Registry, read-only)
    # and shape-validates a manifest, but must not reach execution, policy or
    # the runtime contract, so a draft can never start, merge or claim anything.
    "intake_compile.py": (
        "runtime",
        "cao.runtime",
        "policy",
        "cao.policy",
        "contracts",
        "cao.contracts",
    ),
    # B5 resolution is a read-only projection: it reads existing intake, team
    # and Registry evidence but must not reach execution, policy or the runtime
    # contract, so a derived status can never start, merge or claim anything.
    "intake_resolution.py": (
        "runtime",
        "cao.runtime",
        "policy",
        "cao.policy",
        "contracts",
        "cao.contracts",
    ),
    # B6 handoff is the bridge, but it keeps the same separation: it reaches the
    # existing CAO team owner API behind an authenticated operator gate, never
    # the runtime contract, policy or the generic execution entry points, and it
    # reads the Registry read-only below.
    "intake_handoff.py": (
        "runtime",
        "cao.runtime",
        "policy",
        "cao.policy",
        "contracts",
        "cao.contracts",
    ),
    # B7 queue persists prequalified READ intents and reconciles them into the
    # existing CAO team owner API, but must not reach the runtime contract,
    # policy or the generic execution entry points; its Registry access is
    # separately restricted to read-only calls below.
    "intake_queue.py": (
        "runtime",
        "cao.runtime",
        "policy",
        "cao.policy",
        "contracts",
        "cao.contracts",
    ),
}

# The ONLY Registry attributes advisory triage may touch. Every mutating entry
# point (connect, propose, claim, release, handoff, write_gate, ...) is absent,
# so a triage packet can read active claims but can never write one.
TRIAGE_REGISTRY_ALLOWED = frozenset({
    "APP_DIR",
    "DB_NAME",
    "connect_readonly",
    "list_active_claims",
    "objective_tokens",
    "lexical_similarity",
    "OVERLAP_THRESHOLD",
})

# Modules that would create an egress surface. B3 performs no provider egress:
# with no qualified provider it always yields the deterministic packet.
EGRESS_MODULES = frozenset({
    "socket", "ssl", "urllib", "http", "http.client", "ftplib", "smtplib",
    "telnetlib", "xmlrpc", "asyncio", "requests", "httpx", "aiohttp",
    "urllib3",
})

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


class IntakeStrictnessBoundaryTests(unittest.TestCase):
    """F1 boundary: intake records are read through intake's own strict reader.

    ``cao.state.read_state`` accepts duplicate JSON keys, so intake must not
    route durable records through it. A regression to the permissive reader
    would silently accept a tampered or ambiguous record.
    """

    def test_intake_does_not_import_the_permissive_state_reader(self) -> None:
        imports = _imports(CAO_ROOT / "intake.py")
        self.assertNotIn("state", imports)
        self.assertNotIn("cao.state", imports)

    def test_intake_source_has_no_permissive_state_fallback(self) -> None:
        text = (CAO_ROOT / "intake.py").read_text(encoding="utf-8")
        self.assertNotIn("read_state", text)


class IntakeTriageBoundaryTests(unittest.TestCase):
    """B3 advisory triage: read-only, no egress, no shell, no execution."""

    def _tree(self) -> ast.AST:
        return ast.parse((CAO_ROOT / "intake_triage.py").read_text("utf-8"))

    def test_registry_access_is_readonly_only(self) -> None:
        offenders: set[str] = set()
        for node in ast.walk(self._tree()):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "registry"
            ):
                if node.attr not in TRIAGE_REGISTRY_ALLOWED:
                    offenders.add(node.attr)
        self.assertEqual(offenders, set())

    def test_no_egress_modules(self) -> None:
        imported = _imports(CAO_ROOT / "intake_triage.py")
        offenders = sorted(
            module for module in imported if module in EGRESS_MODULES
        )
        self.assertEqual(offenders, [])

    def test_no_shell_eval_or_dynamic_import(self) -> None:
        offenders: list[str] = []
        for node in ast.walk(self._tree()):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in {
                "eval", "exec", "__import__", "compile",
            }:
                offenders.append(func.id)
            if isinstance(func, ast.Attribute):
                if (
                    func.attr in {"system", "popen"}
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                ):
                    offenders.append(f"os.{func.attr}")
                if func.attr in {"run", "call", "check_output", "Popen"}:
                    for keyword in node.keywords:
                        if keyword.arg == "shell":
                            offenders.append("subprocess shell=True")
        self.assertEqual(offenders, [])

    def test_git_is_reached_only_through_the_validated_argv_choke_point(self) -> None:
        # The module may import the read-only git probe core, but every actual
        # git invocation must go through ``_run_git``. A stray ``subprocess``
        # call outside it would bypass the allowlist.
        tree = self._tree()
        direct = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_run_git":
                for inner in ast.walk(node):
                    if (
                        isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "run"
                    ):
                        direct += 1
        self.assertEqual(direct, 1)

    def test_no_registry_writes_or_ledger_in_triage(self) -> None:
        text = (CAO_ROOT / "intake_triage.py").read_text(encoding="utf-8")
        for forbidden in (
            "registry.connect(",
            "registry.propose",
            "registry.claim(",
            "registry.release",
            "registry.handoff",
            "registry.write_gate",
            "registry.record_observation",
            "registry.record_disposition",
        ):
            self.assertNotIn(forbidden, text)


class IntakeCompileBoundaryTests(unittest.TestCase):
    """B4 routing: read-only, no egress, no shell, no execution or team write.

    A compiled draft is a reviewable proposal, so this module must never reach
    an execution entry point, initialize/start/tick a team, run a product task,
    write a Registry row or touch canonical Company State.
    """

    def _tree(self) -> ast.AST:
        return ast.parse((CAO_ROOT / "intake_compile.py").read_text("utf-8"))

    def test_registry_access_is_readonly_only(self) -> None:
        offenders: set[str] = set()
        for node in ast.walk(self._tree()):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "registry"
            ):
                if node.attr not in TRIAGE_REGISTRY_ALLOWED:
                    offenders.add(node.attr)
        self.assertEqual(offenders, set())

    def test_no_egress_modules(self) -> None:
        imported = _imports(CAO_ROOT / "intake_compile.py")
        offenders = sorted(
            module for module in imported if module in EGRESS_MODULES
        )
        self.assertEqual(offenders, [])

    def test_no_shell_eval_or_dynamic_import(self) -> None:
        offenders: list[str] = []
        for node in ast.walk(self._tree()):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in {
                "eval", "exec", "__import__", "compile",
            }:
                offenders.append(func.id)
            if isinstance(func, ast.Attribute):
                if (
                    func.attr in {"system", "popen"}
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                ):
                    offenders.append(f"os.{func.attr}")
                if func.attr in {"run", "call", "check_output", "Popen"}:
                    for keyword in node.keywords:
                        if keyword.arg == "shell":
                            offenders.append("subprocess shell=True")
        self.assertEqual(offenders, [])

    def test_git_is_reached_only_through_the_validated_choke_points(self) -> None:
        # Every git invocation must go through ``_run_git`` (the argv-checked
        # ``subprocess.run``) or ``_read_blob``'s bounded ``Popen``. A stray
        # call outside them would bypass the allowlist.
        tree = self._tree()
        allowed = {"_run_git": {"run"}, "_read_blob": {"Popen"}}
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and isinstance(inner.func.value, ast.Name)
                    and inner.func.value.id == "subprocess"
                ):
                    if inner.func.attr not in allowed.get(node.name, set()):
                        offenders.append(f"{node.name}: subprocess.{inner.func.attr}")
        self.assertEqual(offenders, [])

    def test_no_team_execution_product_task_or_canonical_write(self) -> None:
        text = (CAO_ROOT / "intake_compile.py").read_text(encoding="utf-8")
        for forbidden in (
            "team.initialize(",
            "team.set_desired(",
            "team.resume(",
            "team.retry_read(",
            "team.tick(",
            "team.tick_all(",
            "product_task.run",
            "state.write_state",
            "registry.connect(",
            "registry.propose",
            "registry.claim(",
            "registry.release",
            "registry.handoff",
            "registry.write_gate",
        ):
            self.assertNotIn(forbidden, text)


class IntakeResolutionBoundaryTests(unittest.TestCase):
    """B5 resolution: read-only, no egress, no shell, no execution or team write.

    A derived status is a projection, so this module must never reach an
    execution entry point, initialize/start/tick a team, run a product task,
    write a Registry row or touch canonical Company State, and it must open no
    egress surface.
    """

    def _tree(self) -> ast.AST:
        return ast.parse((CAO_ROOT / "intake_resolution.py").read_text("utf-8"))

    def test_registry_access_is_readonly_only(self) -> None:
        offenders: set[str] = set()
        for node in ast.walk(self._tree()):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "registry"
            ):
                if node.attr not in TRIAGE_REGISTRY_ALLOWED:
                    offenders.add(node.attr)
        self.assertEqual(offenders, set())

    def test_no_egress_modules(self) -> None:
        imported = _imports(CAO_ROOT / "intake_resolution.py")
        offenders = sorted(
            module for module in imported if module in EGRESS_MODULES
        )
        self.assertEqual(offenders, [])

    def test_no_shell_eval_or_dynamic_import(self) -> None:
        offenders: list[str] = []
        for node in ast.walk(self._tree()):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in {
                "eval", "exec", "__import__", "compile",
            }:
                offenders.append(func.id)
            if isinstance(func, ast.Attribute):
                if (
                    func.attr in {"system", "popen"}
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                ):
                    offenders.append(f"os.{func.attr}")
                if func.attr in {"run", "call", "check_output", "Popen"}:
                    for keyword in node.keywords:
                        if keyword.arg == "shell":
                            offenders.append("subprocess shell=True")
        self.assertEqual(offenders, [])

    def test_no_team_execution_product_task_or_canonical_write(self) -> None:
        text = (CAO_ROOT / "intake_resolution.py").read_text(encoding="utf-8")
        for forbidden in (
            "team.initialize(",
            "team.set_desired(",
            "team.resume(",
            "team.retry_read(",
            "team.tick(",
            "team.tick_all(",
            "product_task.run",
            "state.write_state",
            "registry.connect(",
            "registry.propose",
            "registry.claim(",
            "registry.release",
            "registry.handoff",
            "registry.write_gate",
        ):
            self.assertNotIn(forbidden, text)

    def test_resolution_persists_nothing(self) -> None:
        # A projection may not create a private store or write a record.
        text = (CAO_ROOT / "intake_resolution.py").read_text(encoding="utf-8")
        for forbidden in (
            "open_private_dir(",
            "write_private_record(",
            "write_state(",
            "os.makedirs",
        ):
            self.assertNotIn(forbidden, text)

    def test_reply_accepts_no_caller_supplied_adapter(self) -> None:
        # The mock pathway must have no pluggable adapter: `reply` cannot take a
        # caller-supplied object or callable whose `send` might reach the
        # network while the call still reports sent = False. An unknown keyword
        # such as `adapter` is therefore a TypeError.
        tree = self._tree()
        signatures: dict[str, set[str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "reply":
                names = {arg.arg for arg in node.args.args}
                names |= {arg.arg for arg in node.args.kwonlyargs}
                names |= {arg.arg for arg in (node.args.vararg, node.args.kwarg)
                          if arg is not None}
                signatures[node.name] = names
        self.assertIn("reply", signatures)
        self.assertNotIn("adapter", signatures["reply"])


class IntakeHandoffBoundaryTests(unittest.TestCase):
    """B6 handoff: read-only Registry, no egress, no shell, no shadow work.

    The module hands one immutable READ manifest to the existing CAO team owner
    API, but only behind the authenticated operator gate. It must never reach a
    generic execution entry point, tick/schedule a team, write a Registry row,
    touch canonical Company State or open an egress surface.
    """

    def _tree(self) -> ast.AST:
        return ast.parse((CAO_ROOT / "intake_handoff.py").read_text("utf-8"))

    def test_registry_access_is_readonly_only(self) -> None:
        offenders: set[str] = set()
        for node in ast.walk(self._tree()):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "registry"
            ):
                if node.attr not in TRIAGE_REGISTRY_ALLOWED:
                    offenders.add(node.attr)
        self.assertEqual(offenders, set())

    def test_no_egress_modules(self) -> None:
        imported = _imports(CAO_ROOT / "intake_handoff.py")
        offenders = sorted(
            module for module in imported if module in EGRESS_MODULES
        )
        self.assertEqual(offenders, [])

    def test_no_shell_eval_or_dynamic_import(self) -> None:
        offenders: list[str] = []
        for node in ast.walk(self._tree()):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in {
                "eval", "exec", "__import__", "compile",
            }:
                offenders.append(func.id)
            if isinstance(func, ast.Attribute):
                if (
                    func.attr in {"system", "popen"}
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                ):
                    offenders.append(f"os.{func.attr}")
                if func.attr in {"run", "call", "check_output", "Popen"}:
                    for keyword in node.keywords:
                        if keyword.arg == "shell":
                            offenders.append("subprocess shell=True")
        self.assertEqual(offenders, [])
        text = (CAO_ROOT / "intake_handoff.py").read_text("utf-8")
        self.assertNotIn("subprocess", text)

    @staticmethod
    def _team_mutations(node: ast.AST) -> set[str]:
        found: set[str] = set()
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and isinstance(inner.func.value, ast.Name)
                and inner.func.value.id == "team"
                and inner.func.attr in {"initialize", "set_desired"}
            ):
                found.add(inner.func.attr)
        return found

    def test_team_owner_api_is_reached_only_behind_the_gate(self) -> None:
        # The only team mutation entry points live inside ``_dispatch``, which
        # is called only on the fully-proven authority path.
        tree = self._tree()
        offenders: list[str] = []
        seen: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            calls = self._team_mutations(node)
            if not calls:
                continue
            seen |= calls
            if node.name != "_dispatch":
                offenders.append(node.name)
        self.assertEqual(offenders, [])
        self.assertEqual(seen, {"initialize", "set_desired"})

    def test_no_tick_schedule_or_registry_write(self) -> None:
        source = (CAO_ROOT / "intake_handoff.py").read_text("utf-8")
        for forbidden in (
            "team.tick(",
            "team.tick_all(",
            "registry.connect(",
            "registry.propose",
            "registry.claim(",
            "registry.release",
            "registry.handoff",
            "registry.write_gate",
            "registry.record_observation",
            "state.write_state",
            "product_task.run",
            "os.makedirs",
        ):
            self.assertNotIn(forbidden, source)

    def test_persists_only_through_the_shared_private_store(self) -> None:
        source = (CAO_ROOT / "intake_handoff.py").read_text("utf-8")
        self.assertIn("open_private_dir(", source)
        self.assertIn("write_private_record(", source)
        self.assertNotIn("read_state(", source)
        self.assertNotIn("state.write", source)

    def test_cli_never_constructs_or_accepts_operator_authority(self) -> None:
        cli = (CAO_ROOT / "intake_cli.py").read_text("utf-8")
        # The CLI passes exactly one authority: the literal ``None``. It never
        # reads an operator identity from the environment or a file, and never
        # builds a grant, so a caller cannot assert authority through it.
        self.assertIn("operator_authority=None", cli)
        for forbidden in (
            "CAO_OPERATOR",
            "CAO_HOST_PRINCIPAL",
            "intake_handoff._binding_digest",
            "intake_handoff._authority_verdict",
            "import team",
        ):
            self.assertNotIn(forbidden, cli)


class IntakeQueueBoundaryTests(unittest.TestCase):
    """B7 queue: read-only Registry, no egress, no shell, no shadow work.

    The queue persists prequalified READ intents and reconciles them into the
    existing CAO team owner API, but it must never reach a generic execution
    entry point, write a Registry row, touch canonical Company State or open an
    egress surface, and it must start a team only from the single advance path.
    """

    def _tree(self) -> ast.AST:
        return ast.parse((CAO_ROOT / "intake_queue.py").read_text("utf-8"))

    def test_registry_access_is_readonly_only(self) -> None:
        offenders: set[str] = set()
        for node in ast.walk(self._tree()):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "registry"
            ):
                if node.attr not in TRIAGE_REGISTRY_ALLOWED:
                    offenders.add(node.attr)
        self.assertEqual(offenders, set())

    def test_no_egress_modules(self) -> None:
        imported = _imports(CAO_ROOT / "intake_queue.py")
        offenders = sorted(
            module for module in imported if module in EGRESS_MODULES
        )
        self.assertEqual(offenders, [])

    def test_no_shell_eval_or_dynamic_import(self) -> None:
        offenders: list[str] = []
        for node in ast.walk(self._tree()):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in {
                "eval", "exec", "__import__", "compile",
            }:
                offenders.append(func.id)
            if isinstance(func, ast.Attribute):
                if (
                    func.attr in {"system", "popen"}
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                ):
                    offenders.append(f"os.{func.attr}")
                if func.attr in {"run", "call", "check_output", "Popen"}:
                    for keyword in node.keywords:
                        if keyword.arg == "shell":
                            offenders.append("subprocess shell=True")
        self.assertEqual(offenders, [])
        self.assertNotIn("subprocess",
                         (CAO_ROOT / "intake_queue.py").read_text("utf-8"))

    def test_team_start_is_reached_only_from_the_advance_path(self) -> None:
        # The only team mutation entry point is ``team.set_desired`` inside
        # ``_advance``, called only once every fail-closed gate has proven.
        tree = self._tree()
        offenders: list[str] = []
        seen: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            calls = {
                inner.func.attr
                for inner in ast.walk(node)
                if isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and isinstance(inner.func.value, ast.Name)
                and inner.func.value.id == "team"
                and inner.func.attr in {"initialize", "set_desired"}
            }
            if not calls:
                continue
            seen |= calls
            if node.name != "_advance":
                offenders.append(node.name)
        self.assertEqual(offenders, [])
        self.assertEqual(seen, {"set_desired"})

    def test_no_registry_write_product_task_or_canonical_write(self) -> None:
        source = (CAO_ROOT / "intake_queue.py").read_text("utf-8")
        for forbidden in (
            "registry.connect(",
            "registry.propose",
            "registry.claim(",
            "registry.release",
            "registry.handoff",
            "registry.write_gate",
            "registry.record_observation",
            "product_task.run",
            "team.tick(",
            "team.tick_all(",
        ):
            self.assertNotIn(forbidden, source)


class IntakeCliGuestBoundaryTests(unittest.TestCase):
    """The CLI serves no guest projection from caller-controlled input.

    B5 has no independently authenticated host connector, so ``--guest-view``
    must fail closed. If the CLI reached ``guest_identity``/``guest_projection``
    it would trust a principal or scope a caller can assert through the process
    environment.
    """

    def test_cli_never_serves_a_guest_projection(self) -> None:
        text = (CAO_ROOT / "intake_cli.py").read_text(encoding="utf-8")
        for forbidden in (
            "guest_projection(",
            "guest_identity(",
            "CAO_HOST_PRINCIPAL",
        ):
            self.assertNotIn(forbidden, text)


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
