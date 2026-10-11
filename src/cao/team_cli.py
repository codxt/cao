"""Team Execution CLI, using CAO's existing product task executor."""
from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from . import product_config, product_driver, team, team_approval
from .state import StateError


def _emit(value: object) -> None:
    print(json.dumps(value, sort_keys=True))


def main(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="cao team")
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "init", "proposal", "plan"):
        cmd = subs.add_parser(name, help="inspect or initialize a bounded team manifest")
        cmd.add_argument("--file", required=True)
        if name == "init":
            cmd.add_argument("--signature", help="operator-signed exact proposal file")
    for name in ("start", "pause", "tick", "status"):
        cmd = subs.add_parser(name, help="control or inspect an existing team")
        cmd.add_argument("--id", required=True)
    recovery = subs.add_parser("resume", help="resume an inspected UNKNOWN task without a new execution ID")
    recovery.add_argument("--id", required=True)
    recovery.add_argument("--task", required=True)
    retry = subs.add_parser("retry-read", help="admit a fresh READ attempt after an invalid receipt and verified stop")
    retry.add_argument("--id", required=True)
    retry.add_argument("--task", required=True)
    subs.add_parser("list", help="inspect all stored teams")
    try:
        args = parser.parse_args(list(arguments))
    except SystemExit as exc:
        return int(exc.code)
    try:
        if args.command in ("validate", "init", "proposal", "plan"):
            with open(args.file, encoding="utf-8") as f:
                spec = team.validate_spec(json.load(f))
            if args.command == "validate":
                result = {"schema": "cao.team.validate.v1", "status": "VALID",
                          "team_id": spec["team_id"], "tasks": len(spec["tasks"]),
                          "roles": len(spec["roles"])}
            elif args.command in ("proposal", "plan"):
                config = product_config.load_config()
                if config is None:
                    raise team.TeamError("local CAO product configuration is unavailable")
                message = team_approval.proposal(spec, config)
                if args.command == "proposal":
                    print(message.decode("ascii"), end="")
                    return 0
                result = {"schema": "cao.team.plan.v1",
                          "team_id": spec["team_id"],
                          "initiative": spec.get("initiative"),
                          "supersedes": spec.get("supersedes"),
                          "change_reason": spec.get("change_reason"),
                          "roles": spec["roles"],
                          "tasks": [{k: t.get(k) for k in ("id", "role", "mode",
                                   "depends_on", "reviewer")} for t in spec["tasks"]],
                          "max_writers": spec["max_writers"],
                          "max_parallel": spec["max_parallel"],
                          "approval": json.loads(message)}
            else:
                config = product_config.load_config()
                signature = None
                if args.signature:
                    with open(args.signature, "rb") as f:
                        signature = f.read(16385)
                result = team.initialize(spec, config=config, signature=signature)
        elif args.command == "list":
            result = {"schema": "cao.team.list.v1", "teams": team.list_teams()}
        elif args.command == "status":
            result = team.status(args.id)
        elif args.command == "resume":
            result = team.resume(args.id, args.task)
        elif args.command == "retry-read":
            result = team.retry_read(args.id, args.task)
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
