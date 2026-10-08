"""Command line interface for CAO.

Only the plugin surface is implemented in F0. The lane groups ``work``,
``worker``, ``run``, ``coordinator`` and ``relay`` are scaffolded so their
command shape is stable, but every command under them reports a structured
``UNAVAILABLE`` result and a nonzero exit status. They never pretend success.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from . import __version__
from .runtime import PluginRegistry

__all__ = ["main", "build_parser", "EXIT_OK", "EXIT_UNAVAILABLE", "EXIT_USAGE"]

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3

PLUGIN_GROUP = "plugins"
# Planned lane groups and the command names scaffolded for each. These are
# placeholders owned by later lanes; no lane module exists in F0.
PLANNED_GROUPS: dict[str, tuple[str, ...]] = {
    "work": ("list",),
    "worker": ("list",),
    "run": ("list",),
    "coordinator": ("status",),
    "relay": ("status",),
}


def _cmd_plugins_list(args: argparse.Namespace) -> int:
    registry = PluginRegistry.discover()
    plugins = [entry.as_dict() for entry in registry.entries()]
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


def _cmd_unavailable(args: argparse.Namespace) -> int:
    group = getattr(args, "group", None)
    command = getattr(args, "command", None)
    payload = {
        "schema": "cao.cli.unavailable.v0",
        "status": "UNAVAILABLE",
        "group": group,
        "command": command,
        "error": {
            "code": "LANE_NOT_IMPLEMENTED",
            "verdict": "UNAVAILABLE",
            "message": (
                f"'{group} {command}' is scaffolded but not implemented in F0"
            ),
        },
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return EXIT_UNAVAILABLE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cao",
        description=(
            "CAO - open-source AgentOps control plane for agentic runtimes. "
            "F0 exposes runtime plugin discovery; later lanes add work, "
            "worker, run, coordinator and relay commands."
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

    for group, commands in PLANNED_GROUPS.items():
        group_parser = groups.add_parser(
            group,
            help=f"planned lane: {group} (not implemented in F0)",
            parents=[json_parent],
        )
        group_commands = group_parser.add_subparsers(
            dest="command", metavar="COMMAND"
        )
        for command in commands:
            command_parser = group_commands.add_parser(
                command,
                help=f"planned command: {group} {command} (not implemented in F0)",
                parents=[json_parent],
            )
            command_parser.set_defaults(handler=_cmd_unavailable)

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
