"""Team Execution CLI, using CAO's existing product task executor."""
from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence

from . import product_config, product_driver, team, team_approval
from .state import StateError


def _emit(value: object) -> None:
    print(json.dumps(value, sort_keys=True))


def _route(entry: Mapping[str, object],
           allowed: object = None) -> dict[str, object]:
    """One read-only view of a resolved work/review route.

    The effective selection CAO would dispatch is reported (agent kind and
    provider), never the raw argument vector, environment or other private
    state. A model is shown only as a safe bounded token when the provider
    entry and any argv ``--model`` agree; a divergent, repeated or malformed
    model selection is reported unresolved rather than falsely resolved. The
    operator's configured model allowlist decision is reported separately as
    ``model_allowed`` so the read-only plan reflects the same deny decision
    dispatch enforces without altering the resolved route.
    """

    status, model = product_config.route_model(entry)
    provider = entry.get("provider")
    provider = provider if type(provider) is dict else {}
    return {
        "agent_kind": product_config.safe_route_token(entry.get("agent_kind")),
        "provider": {"cli": product_config.safe_route_token(provider.get("cli")),
                     "id": product_config.safe_route_token(provider.get("id")),
                     "model": product_config.safe_route_variant(
                         provider.get("model"))},
        "model": model,
        "model_status": status,
        "model_allowed": True if allowed is None else allowed,
    }


def _task_plan(task: Mapping[str, object],
               routes: Mapping[str, object],
               allowed: Mapping[str, object]) -> dict[str, object]:
    return {"id": task.get("id"), "role": task.get("role"),
            "mode": task.get("mode"), "depends_on": task.get("depends_on"),
            "reviewer": task.get("reviewer"),
            "routes": {phase: _route(routes[phase], allowed.get(phase))
                       for phase in ("work", "review") if phase in routes}}


def _plan(spec: Mapping[str, object],
          config: product_config.ProductConfig) -> dict[str, object]:
    """The read-only operator plan for one validated manifest.

    Effective routes come from the same resolver ``team.initialize`` uses, so
    the reported digests are exactly the confirmation a chat-mode
    ``cao team init --approved`` would save. Legacy approval mode claims no
    chat-confirmation authority and reports truthful null hashes.
    """

    bound = team._dispatchable_routes(config, spec)
    routes, allowed = bound["routes"], bound["allowed"]
    mode = team_approval.policy(config)["mode"]
    confirmed = (team_approval.confirmation(spec, config, routes)
                 if mode == "chat" and spec.get("initiative") else None)
    return {
        "schema": "cao.team.plan.v1",
        "team_id": spec["team_id"],
        "initiative": spec.get("initiative"),
        "supersedes": spec.get("supersedes"),
        "change_reason": spec.get("change_reason"),
        "roles": {name: {key: role[key]
                         for key in ("model", "agent_profile") if key in role}
                  for name, role in spec["roles"].items()},
        "tasks": [_task_plan(task, routes.get(task["id"], {}),
                             allowed.get(task["id"], {}))
                  for task in spec["tasks"]],
        "max_writers": spec["max_writers"],
        "max_parallel": spec["max_parallel"],
        "approval_mode": mode,
        "spec_sha256": confirmed["spec_sha256"] if confirmed else None,
        "routes_sha256": confirmed["routes_sha256"] if confirmed else None,
    }


def main(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="cao team")
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "init", "plan"):
        cmd = subs.add_parser(name, help="inspect or initialize a bounded team manifest")
        cmd.add_argument("--file", required=True)
        if name == "init":
            cmd.add_argument("--approved", action="store_true", help="confirm that the operator approved this team")
    for name in ("start", "pause", "tick", "status"):
        cmd = subs.add_parser(name, help="control or inspect an existing team")
        cmd.add_argument("--id", required=True)
    recovery = subs.add_parser("resume", help="resume an inspected UNKNOWN task without a new execution ID")
    recovery.add_argument("--id", required=True)
    recovery.add_argument("--task", required=True)
    retry = subs.add_parser("retry-read", help="admit a fresh READ attempt (work or review) after an invalid receipt and verified stop")
    retry.add_argument("--id", required=True)
    retry.add_argument("--task", required=True)
    subs.add_parser("list", help="inspect all stored teams")
    subs.add_parser(
        "backlog",
        help="group stored teams by initiative and successor lineage (read-only)")
    try:
        args = parser.parse_args(list(arguments))
    except SystemExit as exc:
        return int(exc.code)
    try:
        if args.command in ("validate", "init", "plan"):
            with open(args.file, encoding="utf-8") as f:
                spec = team.validate_spec(json.load(f))
            if args.command == "validate":
                result = {"schema": "cao.team.validate.v1", "status": "VALID",
                          "team_id": spec["team_id"], "tasks": len(spec["tasks"]),
                          "roles": len(spec["roles"])}
            elif args.command == "plan":
                config = product_config.load_config()
                if config is None:
                    raise team.TeamError("local CAO product configuration is unavailable")
                result = _plan(spec, config)
            else:
                config = product_config.load_config()
                result = team.initialize(spec, config=config, approved=args.approved)
        elif args.command == "list":
            result = {"schema": "cao.team.list.v1", "teams": team.list_teams()}
        elif args.command == "backlog":
            result = team.backlog()
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
