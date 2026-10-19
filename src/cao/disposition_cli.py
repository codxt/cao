"""Command line for the standalone CAO disposition control.

Usage::

    cao-disposition plan --team-id <registered-team>
    cao-disposition authorize --team-id <registered-team>
    cao-disposition apply --team-id <registered-team>
    cao-disposition verify --team-id <registered-team>

Only the four verbs and the single ``--team-id`` input exist. There is no
``--run-id``, no ``--authority``, no protected-path or UID override and no shell
command. The ordinary ``cao`` product CLI is untouched; this console command is
installed separately by the project's ``cao-disposition`` script entry.

Exit codes: ``0`` success, ``2`` usage/refusal, ``3`` explicit UNAVAILABLE.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import disposition_control, product_config, registry, state, team

__all__ = ["main", "EXIT_OK", "EXIT_USAGE", "EXIT_UNAVAILABLE"]

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3

ERROR_SCHEMA = "cao.disposition.error.v1"

_VERBS = ("plan", "authorize", "apply", "verify")


def _emit(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _error(command: str, status: str, code: str, message: str) -> int:
    _emit({
        "schema": ERROR_SCHEMA,
        "status": status,
        "command": command,
        "error": {
            "code": code,
            "verdict": status,
            "message": message,
        },
    })
    return EXIT_UNAVAILABLE if status == "UNAVAILABLE" else EXIT_USAGE


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cao-disposition",
        description=(
            "Authorize and apply independent run-local Work Registry "
            "dispositions for one paused CAO team. The protected operator "
            "grant is external; with no grant the command reports UNAVAILABLE."
        ),
    )
    subcommands = parser.add_subparsers(dest="command", metavar="VERB",
                                       required=True)
    for verb in _VERBS:
        sub = subcommands.add_parser(
            verb,
            help={
                "plan": "read-only preflight and canonical plan digest",
                "authorize": "bind the protected grant to the current plan",
                "apply": "apply all approved dispositions atomically",
                "verify": "read-only re-probe and readiness report",
            }[verb],
        )
        sub.add_argument("--team-id", required=True,
                         help="an existing immutable PAUSED CAO team id")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = args.command
    handler = {
        "plan": disposition_control.plan,
        "authorize": disposition_control.authorize,
        "apply": disposition_control.apply,
        "verify": disposition_control.verify,
    }[command]
    try:
        payload = handler(args.team_id)
    except disposition_control.DispositionUnavailable as exc:
        return _error(command, exc.status, exc.code, str(exc))
    except disposition_control.DispositionRefused as exc:
        return _error(command, exc.status, exc.code, str(exc))
    except disposition_control.DispositionError as exc:
        return _error(command, exc.status, exc.code, str(exc))
    except (team.TeamError, registry.RegistryError,
            product_config.ProductConfigError, state.StateError) as exc:
        return _error(command, "REFUSED",
                      str(getattr(exc, "code", "DISPOSITION_REFUSED")),
                      str(exc))
    _emit(payload)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
