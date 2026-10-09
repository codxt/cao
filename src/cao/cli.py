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
AUTOMATION_GROUP = "automation"


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


def _read_json_document(path: str, group: str, command: str) -> tuple[Any, int | None]:
    """Read one JSON object from an explicitly named file, or fail closed.

    An unreadable, absent, non-JSON or non-object file is a fail-closed result.
    It is never treated as an empty or default document, because every document
    this command group reads carries authority-bearing refusals.
    """

    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        return None, _unavailable(
            group, command, "DOCUMENT_UNREADABLE", f"{type(exc).__name__}: {exc}"
        )
    except ValueError as exc:
        return None, _unavailable(
            group, command, "DOCUMENT_MALFORMED", f"{type(exc).__name__}: {exc}"
        )
    if not isinstance(payload, dict):
        return None, _unavailable(
            group,
            command,
            "DOCUMENT_SCHEMA",
            f"document must contain a JSON object, got {type(payload).__name__}",
        )
    return payload, None


def _cmd_automation_validate(args: argparse.Namespace) -> int:
    """Validate an automation manifest and its owner preferences, read-only."""

    manifest, code = _read_json_document(
        args.config, AUTOMATION_GROUP, "validate"
    )
    if manifest is None:
        return code if code is not None else EXIT_UNAVAILABLE

    preferences = None
    if args.preferences is not None:
        preferences, code = _read_json_document(
            args.preferences, AUTOMATION_GROUP, "validate"
        )
        if preferences is None:
            return code if code is not None else EXIT_UNAVAILABLE

    try:
        from .spec import SpecInvalid, load_preferences, load_spec

        automation = load_spec(manifest)
    except SpecInvalid as exc:
        _emit(
            {
                "schema": "cao.cli.automation.v0",
                "status": "INVALID",
                "config": args.config,
                "error": {
                    "code": exc.code,
                    "verdict": exc.verdict.value,
                    "field": exc.field,
                    "message": exc.reason,
                },
            }
        )
        return EXIT_UNAVAILABLE

    preference_state = "not_supplied"
    if preferences is not None:
        try:
            load_preferences(preferences)
        except SpecInvalid as exc:
            _emit(
                {
                    "schema": "cao.cli.automation.v0",
                    "status": "INVALID",
                    "config": args.config,
                    "preferences": args.preferences,
                    "error": {
                        "code": exc.code,
                        "verdict": exc.verdict.value,
                        "field": exc.field,
                        "message": exc.reason,
                    },
                }
            )
            return EXIT_UNAVAILABLE
        preference_state = "valid"

    _emit(
        {
            "schema": "cao.cli.automation.v0",
            "status": "OK",
            "config": args.config,
            "version": automation.version,
            "name": automation.name,
            "timezone": automation.timezone,
            "source_count": len(automation.sources),
            "source_ids": [source.source_id for source in automation.sources],
            "destination": {
                "sink": automation.destination.sink,
                "destination_id": automation.destination.destination_id,
                "cost_measurement": automation.destination.cost_measurement,
            },
            "schedule": {
                "cadence": automation.schedule.cadence,
                "executed_by": "an external scheduler the owner runs; CAO never fires it",
            },
            "limits": {
                "max_duration_seconds": automation.limits.max_duration_seconds,
                "max_concurrency": automation.limits.max_concurrency,
                "max_calls": automation.limits.max_calls,
            },
            "stop": {"state": automation.stop.state.value},
            "preferences": preference_state,
        }
    )
    return EXIT_OK


def _automation_state_summary(name: str, persisted: dict[str, Any]) -> dict[str, Any]:
    """Summarize one persisted automation state file through its own loader."""

    if name == "bookmarks":
        from .sources import load_bookmarks

        loaded = load_bookmarks(persisted)
        return {
            "status": "OK",
            "sources": sorted(persisted["bookmarks"]),
            "max_delivered_per_source": loaded.max_delivered_per_source,
        }
    if name == "delivery":
        from .delivery import load_ledger

        loaded = load_ledger(persisted)
        return {
            "status": "OK",
            "receipt_count": len(loaded),
            "pending": [
                {"key": receipt.key, "state": receipt.state.value}
                for receipt in loaded.pending()
            ],
        }
    from .brief import load_history

    loaded = load_history(persisted)
    return {
        "status": "OK",
        "observed_count": len(loaded),
        "max_entries": loaded.max_entries,
    }


def _cmd_automation_status(args: argparse.Namespace) -> int:
    """Report persisted automation state truthfully, read-only.

    An absent state file is a fail-closed answer, never an empty success: the
    difference between "no run has happened" and "this is what the last run
    actually observed" is the whole point of this command.
    """

    states: dict[str, Any] = {}
    unavailable: list[str] = []
    for name, path in (
        ("bookmarks", args.bookmarks),
        ("delivery", args.delivery),
        ("brief_history", args.history),
    ):
        if path is None:
            states[name] = "not_supplied"
            continue
        try:
            persisted = state_module.read_state(path)
            if persisted is None:
                states[name] = "absent"
                unavailable.append(name)
                continue
            states[name] = _automation_state_summary(name, persisted)
        except Exception as exc:  # noqa: BLE001 - unreadable state fails closed
            states[name] = {
                "status": "INVALID",
                "error": f"{type(exc).__name__}: {exc}",
            }
            unavailable.append(name)

    _emit(
        {
            "schema": "cao.cli.automation.status.v0",
            "status": "OK" if not unavailable else "UNAVAILABLE",
            "state": states,
        }
    )
    return EXIT_UNAVAILABLE if unavailable else EXIT_OK


def _cmd_automation_run(args: argparse.Namespace) -> int:
    """Validate the manifest, then report that no concrete adapter ships.

    The manifest is read and validated first, because a caller who passes a
    broken manifest deserves to be told that rather than told about a missing
    adapter. Only when the manifest is valid does the honest answer become the
    missing capability: composing a brief needs a ``SourceReader`` for every
    declared source, and CAO ships none. No runtime and no source adapter is a
    built-in default.
    """

    manifest, code = _read_json_document(args.config, AUTOMATION_GROUP, "run")
    if manifest is None:
        return code if code is not None else EXIT_UNAVAILABLE
    try:
        from .spec import SpecInvalid, load_spec

        load_spec(manifest)
    except SpecInvalid as exc:
        _emit(
            {
                "schema": "cao.cli.automation.v0",
                "status": "INVALID",
                "config": args.config,
                "error": {
                    "code": exc.code,
                    "verdict": exc.verdict.value,
                    "field": exc.field,
                    "message": exc.reason,
                },
            }
        )
        return EXIT_UNAVAILABLE

    return _unavailable(
        AUTOMATION_GROUP,
        "run",
        "NO_SOURCE_ADAPTER",
        "the manifest is valid, but no concrete SourceReader is registered for "
        "this build, so nothing can be executed; CAO ships no runtime and no "
        "source adapter as a default. Validate a manifest with 'cao automation "
        "validate' and inspect persisted state with 'cao automation status'.",
    )


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

    automation = groups.add_parser(
        AUTOMATION_GROUP,
        help="validate and inspect declarative automations (read-only)",
        parents=[json_parent],
    )
    automation_commands = automation.add_subparsers(
        dest="command", metavar="COMMAND"
    )
    automation_validate = automation_commands.add_parser(
        "validate",
        help="validate an automation manifest and its owner preferences",
        parents=[json_parent],
    )
    automation_validate.add_argument(
        "--config", required=True, metavar="PATH", help="automation manifest JSON"
    )
    automation_validate.add_argument(
        "--preferences",
        default=None,
        metavar="PATH",
        help="owner preferences JSON to validate read-only (never written)",
    )
    automation_validate.set_defaults(handler=_cmd_automation_validate)

    automation_status = automation_commands.add_parser(
        "status",
        help="report persisted automation state through its own loader",
        parents=[json_parent],
    )
    for flag, help_text in (
        ("--bookmarks", "per-source bookmark state JSON (cao.automation.sources.v0)"),
        ("--delivery", "delivery receipt ledger JSON (cao.automation.delivery.v0)"),
        ("--history", "brief observation history JSON (cao.automation.brief.history.v0)"),
    ):
        automation_status.add_argument(
            flag, default=None, metavar="PATH", help=help_text
        )
    automation_status.set_defaults(handler=_cmd_automation_status)

    automation_run = automation_commands.add_parser(
        "run",
        help="report that no concrete source adapter ships in this build",
        parents=[json_parent],
    )
    automation_run.add_argument(
        "--config", required=True, metavar="PATH", help="automation manifest JSON"
    )
    automation_run.add_argument(
        "--dry-run",
        action="store_true",
        help="accepted and reported truthfully; there is nothing to execute",
    )
    automation_run.set_defaults(handler=_cmd_automation_run)

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
