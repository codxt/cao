"""Command line interface for CAO.

Every command is read-only. The CLI never mutates state and never invents a
result: it calls the module that owns a piece of state and reports exactly what
that module reports. A missing or unreadable state source exits nonzero with
explicit structured evidence (``cao.cli.unavailable.v0``) rather than an
empty-but-successful payload.

Output is structured JSON on stdout with a stable ``schema`` field. Exit codes
are the frozen convention: ``0`` success, ``2`` usage error, ``3`` explicit
unavailable/unknown (fail closed).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from . import __version__, registry
from . import coordinator as coordinator_module
from . import relay as relay_module
from . import state as state_module
from .evidence import EvidenceSource
from .runtime import PluginRegistry

__all__ = ["main", "build_parser", "EXIT_OK", "EXIT_UNAVAILABLE", "EXIT_USAGE"]

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3

PLUGIN_GROUP = "plugins"


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _unavailable(group: str, command: str, code: str, message: str) -> int:
    """Emit a fail-closed result and return the unavailable exit code."""

    _emit(
        {
            "schema": "cao.cli.unavailable.v0",
            "status": "UNAVAILABLE",
            "group": group,
            "command": command,
            "error": {
                "code": code,
                "verdict": "UNAVAILABLE",
                "message": message,
            },
        }
    )
    return EXIT_UNAVAILABLE


def _read_registry(group: str, command: str, path: str):
    """Open an existing registry ledger read-only, or fail closed.

    The ledger is opened through :func:`cao.registry.connect_readonly`, which
    never creates or migrates a database. Any failure to open an explicitly
    named ledger is reported as an unavailable result, never as an empty one.
    """

    try:
        return registry.connect_readonly(path), None
    except Exception as exc:  # noqa: BLE001 - any unreadable ledger fails closed
        return None, _unavailable(
            group,
            command,
            "REGISTRY_UNAVAILABLE",
            f"{type(exc).__name__}: {exc}",
        )


def _cmd_plugins_list(args: argparse.Namespace) -> int:
    registry_entries = PluginRegistry.discover()
    plugins = [entry.as_dict() for entry in registry_entries.entries()]
    payload = {
        "schema": "cao.cli.plugins.v0",
        "status": "OK",
        "plugins": plugins,
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif not plugins:
        print("no runtime plugins registered")
    else:
        for entry in plugins:
            descriptor = entry["descriptor"] or {}
            state = "available" if entry["available"] else "unavailable"
            print(f"{entry['id']}\t{state}\t{descriptor.get('name', '')}")
    return EXIT_OK


def _cmd_work_list(args: argparse.Namespace) -> int:
    ledger = args.ledger or registry.default_db_path()
    conn, code = _read_registry("work", "list", ledger)
    if conn is None:
        return code
    try:
        works = registry.list_works(conn, run_id=args.run)
    except Exception as exc:  # noqa: BLE001 - a failed query fails closed
        return _unavailable(
            "work",
            "list",
            "REGISTRY_UNAVAILABLE",
            f"{type(exc).__name__}: {exc}",
        )
    finally:
        conn.close()
    _emit(
        {
            "schema": "cao.cli.work.v0",
            "status": "OK",
            "ledger": ledger,
            "run_id": args.run,
            "count": len(works),
            "works": works,
        }
    )
    return EXIT_OK


def _cmd_worker_list(args: argparse.Namespace) -> int:
    ledger = args.ledger or registry.default_db_path()
    conn, code = _read_registry("worker", "list", ledger)
    if conn is None:
        return code
    try:
        claims = registry.list_active_claims(conn, run_id=args.run)
    except Exception as exc:  # noqa: BLE001 - a failed query fails closed
        return _unavailable(
            "worker",
            "list",
            "REGISTRY_UNAVAILABLE",
            f"{type(exc).__name__}: {exc}",
        )
    finally:
        conn.close()
    _emit(
        {
            "schema": "cao.cli.worker.v0",
            "status": "OK",
            "ledger": ledger,
            "run_id": args.run,
            "count": len(claims),
            "workers": claims,
        }
    )
    return EXIT_OK


def _cmd_run_list(args: argparse.Namespace) -> int:
    ledger = args.ledger or registry.default_db_path()
    conn, code = _read_registry("run", "list", ledger)
    if conn is None:
        return code
    try:
        works = registry.list_works(conn)
    except Exception as exc:  # noqa: BLE001 - a failed query fails closed
        return _unavailable(
            "run",
            "list",
            "REGISTRY_UNAVAILABLE",
            f"{type(exc).__name__}: {exc}",
        )
    finally:
        conn.close()
    grouped: dict[str, list[str]] = {}
    for work in works:
        grouped.setdefault(work["run_id"], []).append(work["work_key"])
    runs = [
        {
            "run_id": run_id,
            "work_count": len(work_keys),
            "work_keys": sorted(work_keys),
        }
        for run_id, work_keys in grouped.items()
    ]
    _emit(
        {
            "schema": "cao.cli.run.v0",
            "status": "OK",
            "ledger": ledger,
            "derived_from": "registry.list_works",
            "count": len(runs),
            "runs": runs,
        }
    )
    return EXIT_OK


def _cmd_coordinator_status(args: argparse.Namespace) -> int:
    ledger = args.ledger or registry.default_db_path()
    conn, code = _read_registry("coordinator", "status", ledger)
    if conn is None:
        return code
    try:
        claims = registry.list_active_claims(conn, run_id=args.run)
    except Exception as exc:  # noqa: BLE001 - a failed query fails closed
        return _unavailable(
            "coordinator",
            "status",
            "REGISTRY_UNAVAILABLE",
            f"{type(exc).__name__}: {exc}",
        )
    finally:
        conn.close()

    intended = [
        coordinator_module.IntendedState(
            subject=(
                f"{claim['run_id']}/{claim['work_key']}"
                f"#{claim['issue']}@{claim['owner']}"
            ),
            state=claim["mode"],
        )
        for claim in claims
    ]

    def probe(subject: str):
        # No runtime observation source is configured in this lane, so the
        # observed state is genuinely unavailable. The coordinator records
        # UNKNOWN with qualified evidence instead of assuming agreement.
        raise coordinator_module.ProbeUnavailable(
            "no runtime observation source is configured"
        )

    reconciliation = coordinator_module.reconcile(
        intended, probe, source=EvidenceSource.CLI
    )
    counts = reconciliation.verdict_counts()
    unknown = counts.get("UNKNOWN", 0)
    status = "UNKNOWN" if unknown else "OK"
    _emit(
        {
            "schema": "cao.cli.coordinator.v0",
            "status": status,
            "ledger": ledger,
            "run_id": args.run,
            "counts": counts,
            "subjects": [subject.as_dict() for subject in reconciliation.subjects],
        }
    )
    return EXIT_UNAVAILABLE if unknown else EXIT_OK


def _cmd_relay_status(args: argparse.Namespace) -> int:
    path = args.state
    try:
        persisted = state_module.read_state(path)
    except Exception as exc:  # noqa: BLE001 - an unreadable state file fails closed
        return _unavailable(
            "relay",
            "status",
            "RELAY_STATE_UNREADABLE",
            f"{type(exc).__name__}: {exc}",
        )
    if persisted is None:
        return _unavailable(
            "relay",
            "status",
            "RELAY_STATE_NOT_FOUND",
            f"no relay deduplication state file at {path}",
        )
    try:
        ledger = relay_module.load_ledger(persisted)
    except Exception as exc:  # noqa: BLE001 - a malformed ledger fails closed
        return _unavailable(
            "relay",
            "status",
            "RELAY_STATE_INVALID",
            f"{type(exc).__name__}: {exc}",
        )
    _emit(
        {
            "schema": "cao.cli.relay.v0",
            "status": "OK",
            "state": path,
            "max_identities": ledger.max_identities,
            "delivered_count": len(ledger.delivered),
            "delivered": list(ledger.delivered),
            "writers": ledger.writers,
        }
    )
    return EXIT_OK


def _add_ledger_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ledger",
        default=None,
        metavar="PATH",
        help=(
            "registry SQLite ledger to read (default: the registry module's own "
            "default state path)"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cao",
        description=(
            "CAO - open-source AgentOps control plane for agentic runtimes. "
            "All commands are read-only and report structured JSON. Runtime "
            "plugins are selected explicitly by id; no plugin is a built-in "
            "default."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"cao {__version__}"
    )
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON output"
    )

    json_parent = argparse.ArgumentParser(add_help=False)
    json_parent.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="emit machine-readable JSON output",
    )

    groups = parser.add_subparsers(dest="group", metavar="GROUP")

    plugins = groups.add_parser(
        PLUGIN_GROUP,
        help="select and inspect runtime plugins",
        parents=[json_parent],
    )
    plugins_commands = plugins.add_subparsers(dest="command", metavar="COMMAND")
    plugins_list = plugins_commands.add_parser(
        "list",
        help="list registered runtime plugins (explicit ids only)",
        parents=[json_parent],
    )
    plugins_list.set_defaults(handler=_cmd_plugins_list)

    work = groups.add_parser(
        "work",
        help="inspect the work registry ledger (read-only)",
        parents=[json_parent],
    )
    work_commands = work.add_subparsers(dest="command", metavar="COMMAND")
    work_list = work_commands.add_parser(
        "list",
        help="list proposed work items from the registry ledger",
        parents=[json_parent],
    )
    _add_ledger_argument(work_list)
    work_list.add_argument("--run", default=None, help="restrict to one run id")
    work_list.set_defaults(handler=_cmd_work_list)

    worker = groups.add_parser(
        "worker",
        help="inspect active claims in the work registry ledger (read-only)",
        parents=[json_parent],
    )
    worker_commands = worker.add_subparsers(dest="command", metavar="COMMAND")
    worker_list = worker_commands.add_parser(
        "list",
        help="list active claims (owners holding work) from the registry ledger",
        parents=[json_parent],
    )
    _add_ledger_argument(worker_list)
    worker_list.add_argument("--run", default=None, help="restrict to one run id")
    worker_list.set_defaults(handler=_cmd_worker_list)

    run = groups.add_parser(
        "run",
        help="inspect runs by the work they contain (read-only)",
        parents=[json_parent],
    )
    run_commands = run.add_subparsers(dest="command", metavar="COMMAND")
    run_list = run_commands.add_parser(
        "list",
        help="list runs derived from the registry ledger's proposed work",
        parents=[json_parent],
    )
    _add_ledger_argument(run_list)
    run_list.set_defaults(handler=_cmd_run_list)

    coordinator = groups.add_parser(
        "coordinator",
        help="reconcile intended state against observed state (read-only)",
        parents=[json_parent],
    )
    coordinator_commands = coordinator.add_subparsers(
        dest="command", metavar="COMMAND"
    )
    coordinator_status = coordinator_commands.add_parser(
        "status",
        help="reconcile active registry claims against observed state",
        parents=[json_parent],
    )
    _add_ledger_argument(coordinator_status)
    coordinator_status.add_argument(
        "--run", default=None, help="restrict to one run id"
    )
    coordinator_status.set_defaults(handler=_cmd_coordinator_status)

    relay = groups.add_parser(
        "relay",
        help="inspect relay deduplication state (read-only)",
        parents=[json_parent],
    )
    relay_commands = relay.add_subparsers(dest="command", metavar="COMMAND")
    relay_status = relay_commands.add_parser(
        "status",
        help="report a relay deduplication state file (cao.relay.v0)",
        parents=[json_parent],
    )
    relay_status.add_argument(
        "--state",
        required=True,
        metavar="PATH",
        help="relay deduplication state JSON file to read",
    )
    relay_status.set_defaults(handler=_cmd_relay_status)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return EXIT_OK
    return handler(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
