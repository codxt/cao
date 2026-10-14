"""The ``cao intake`` command group: durable admission, no execution.

``submit`` admits one bounded submission file and acknowledges its durable id;
``list`` reports public-safe metadata and never a body; ``show`` reads one full
record from the operator's own private state. ``pull`` is the inbound path: it
admits one submission delivered over a registered transport, reading the
document from a file or stdin. The only registered transport is the marketplace
transport (:mod:`cao.intake_bridge`); any other named transport fails closed
with structured UNAVAILABLE, and ``pull`` with no transport named reports
``NO_INBOUND_TRANSPORT`` rather than inventing a default.

Output is JSON on stdout. Exit codes are ``0`` success, ``2`` usage error and
``3`` explicit UNAVAILABLE/conflict, matching the rest of CAO's CLI.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from . import intake, intake_bridge
from .state import StateError

__all__ = ["main", "build_parser", "EXIT_OK", "EXIT_USAGE", "EXIT_UNAVAILABLE"]

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3

ERROR_SCHEMA = "cao.intake.error.v1"
LIST_SCHEMA = "cao.intake.list.v1"
SHOW_SCHEMA = "cao.intake.show.v1"


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False))


def _error(command: str, code: str, message: str) -> int:
    _emit(
        {
            "schema": ERROR_SCHEMA,
            "status": "UNAVAILABLE",
            "command": command,
            "error": {"code": code, "verdict": "UNAVAILABLE", "message": message},
        }
    )
    return EXIT_UNAVAILABLE


def _cmd_submit(args: argparse.Namespace) -> int:
    try:
        document = intake.load_submission_file(args.file)
        result = intake.submit(document)
    except intake.IntakeError as exc:
        return _error("submit", exc.code, str(exc))
    except (StateError, OSError, UnicodeError, ValueError) as exc:
        return _error("submit", "INTAKE_ERROR", type(exc).__name__)
    _emit(result)
    return EXIT_OK


def _cmd_list(args: argparse.Namespace) -> int:
    try:
        submissions = intake.list_submissions()
    except intake.IntakeError as exc:
        return _error("list", exc.code, str(exc))
    except (StateError, OSError, UnicodeError, ValueError) as exc:
        return _error("list", "INTAKE_ERROR", type(exc).__name__)
    _emit(
        {
            "schema": LIST_SCHEMA,
            "status": "OK",
            "count": len(submissions),
            "submissions": submissions,
        }
    )
    return EXIT_OK


def _cmd_show(args: argparse.Namespace) -> int:
    try:
        record = intake.read_submission(args.submission_id)
    except intake.IntakeError as exc:
        return _error("show", exc.code, str(exc))
    except (StateError, OSError, UnicodeError, ValueError) as exc:
        return _error("show", "INTAKE_ERROR", type(exc).__name__)
    _emit({"schema": SHOW_SCHEMA, "status": "OK", "submission": record})
    return EXIT_OK


def _stdin_stream() -> Any:
    """The binary stdin stream, or stdin itself when it is already binary."""

    return getattr(sys.stdin, "buffer", sys.stdin)


def _cmd_pull(args: argparse.Namespace) -> int:
    transport = getattr(args, "transport", None)
    if not transport:
        return _error(
            "pull",
            "NO_INBOUND_TRANSPORT",
            "no inbound transport was named; pass --transport "
            f"{intake_bridge.TRANSPORT_MARKETPLACE!r} "
            "(see 'cao intake transports')",
        )
    try:
        # The transport is validated *before* any input is touched, so an
        # unsupported or deferred transport is refused without opening the
        # file or consuming stdin.
        intake_bridge.check_transport(transport)
        if getattr(args, "file", None):
            document = intake.load_submission_file(args.file)
            result = intake_bridge.admit_document(document, transport=transport)
        else:
            result = intake_bridge.admit_stream(_stdin_stream(), transport=transport)
    except intake.IntakeError as exc:
        return _error("pull", exc.code, str(exc))
    except (StateError, OSError, UnicodeError, ValueError) as exc:
        return _error("pull", "INTAKE_ERROR", type(exc).__name__)
    _emit(result)
    return EXIT_OK


def _cmd_transports(args: argparse.Namespace) -> int:
    _emit(intake_bridge.bridge_contract())
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cao intake",
        description="Durable private intake admission (no execution).",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    submit = commands.add_parser(
        "submit", help="admit one bounded submission JSON file"
    )
    submit.add_argument("--file", required=True, metavar="PATH",
                        help="strict submission JSON to admit")
    submit.set_defaults(handler=_cmd_submit)

    listing = commands.add_parser(
        "list", help="list public-safe submission metadata (no bodies)"
    )
    listing.set_defaults(handler=_cmd_list)

    show = commands.add_parser("show", help="read one full submission record")
    show.add_argument("submission_id", metavar="SUBMISSION_ID")
    show.set_defaults(handler=_cmd_show)

    pull = commands.add_parser(
        "pull", help="admit one inbound submission over a registered transport"
    )
    pull.add_argument(
        "--transport", metavar="NAME",
        help="registered inbound transport (see 'cao intake transports')",
    )
    pull.add_argument(
        "--file", metavar="PATH",
        help="read the submission from PATH instead of stdin",
    )
    pull.set_defaults(handler=_cmd_pull)

    transports = commands.add_parser(
        "transports", help="show the inbound transport contract and status"
    )
    transports.set_defaults(handler=_cmd_transports)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args_list = [arg for arg in (argv or ()) if arg != "--json"]
    parser = build_parser()
    try:
        args = parser.parse_args(args_list)
    except SystemExit:
        return _error("intake", "USAGE", "invalid 'intake' arguments")
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return EXIT_USAGE
    return handler(args)
