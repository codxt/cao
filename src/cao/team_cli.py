"""Team Execution CLI, using CAO's existing product task executor."""
from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from . import product_config, product_driver, team
from .state import StateError


def _emit(value: object) -> None:
    print(json.dumps(value, sort_keys=True))


def main(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="cao team")
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "init"):
        cmd = subs.add_parser(name, help="validate or initialize a bounded team manifest")
        cmd.add_argument("--file", required=True)
    for name in ("start", "pause", "tick", "status"):
        cmd = subs.add_parser(name, help="control or inspect an existing team")
        cmd.add_argument("--id", required=True)
    recovery = subs.add_parser("resume", help="resume an inspected UNKNOWN task without a new execution ID")
    recovery.add_argument("--id", required=True)
    recovery.add_argument("--task", required=True)
    subs.add_parser("list", help="inspect all stored teams")
    try:
        args = parser.parse_args(list(arguments))
    except SystemExit as exc:
        return int(exc.code)
    try:
        if args.command in ("validate", "init"):
            with open(args.file, encoding="utf-8") as f:
                spec = team.validate_spec(json.load(f))
            if args.command == "validate":
                result = {"schema": "cao.team.validate.v1", "status": "VALID",
                          "team_id": spec["team_id"], "tasks": len(spec["tasks"]),
                          "roles": len(spec["roles"])}
            else:
                result = team.initialize(spec)
        elif args.command == "list":
            result = {"schema": "cao.team.list.v1", "teams": team.list_teams()}
        elif args.command == "status":
            result = team.status(args.id)
        elif args.command == "resume":
            result = team.resume(args.id, args.task)
        elif args.command in ("start", "pause"):
            result = team.set_desired(
                args.id, "running" if args.command == "start" else "paused")
        else:
            # A manual tick is a real task effect, not a read-only command.
            from . import product_cli
            config = product_config.load_config()
            if config is None:
                raise team.TeamError("CAO product provider configuration is unavailable")
            executable, reason = product_cli._managed_executable()
            if executable is None:
                raise team.TeamError("CAO managed runtime unavailable: " + (reason or ""))
            driver = product_driver.RuntimeDriver(executable=executable)
            result = team.tick(args.id, config=config, driver=driver)
    except (OSError, ValueError, StateError, product_config.ProductConfigError) as exc:
        _emit({"schema": "cao.team.error.v1", "status": "UNAVAILABLE",
               "code": getattr(exc, "code", type(exc).__name__)})
        return 3
    _emit(result)
    return 0
