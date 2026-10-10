"""The product-facing CAO command line for a source install.

``./cao`` dispatches here. The commands that the source bootstrap owns answer
today:

``doctor``
    Exact per-subsystem readiness evidence. Fail-closed: anything that cannot be
    positively established stays ``UNKNOWN``/``UNAVAILABLE`` and the verdict is
    ``NOT_READY`` with a nonzero exit.

``status``
    The durable service record. On a fresh clone no record exists, so the
    service is honestly reported ``NOT_STARTED``; no running service is ever
    invented.

``run`` / ``start`` / ``stop`` / ``logs``
    Explicitly ``UNAVAILABLE`` in this build. The task-running lifecycle is
    owned by later lanes; this build never fakes it.

Output is structured JSON on stdout. Exit codes are ``0`` success, ``2`` usage
error, and ``3`` explicit ``UNAVAILABLE``/``NOT_READY``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from . import bootstrap, product_state
from .state import StateError

__all__ = ["main", "build_parser", "EXIT_OK", "EXIT_USAGE", "EXIT_UNAVAILABLE"]

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3

STATUS_SCHEMA = "cao.product.status.v0"
UNAVAILABLE_SCHEMA = "cao.product.unavailable.v0"


def _emit(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _unavailable(command: str, code: str, message: str) -> int:
    _emit(
        {
            "schema": UNAVAILABLE_SCHEMA,
            "status": "UNAVAILABLE",
            "command": command,
            "error": {"code": code, "verdict": "UNAVAILABLE", "message": message},
        }
    )
    return EXIT_UNAVAILABLE


def _doctor(args: argparse.Namespace) -> int:
    payload, code = bootstrap.doctor()
    _emit(payload)
    return code


def _cmd_status(args: argparse.Namespace) -> int:
    try:
        record = product_state.read_service()
    except StateError as exc:
        return _unavailable(
            "status",
            "SERVICE_STATE_UNREADABLE",
            f"{type(exc).__name__}: {exc}",
        )
    if record is None:
        _emit(
            {
                "schema": STATUS_SCHEMA,
                "status": "NOT_STARTED",
                "desired": "stopped",
                "observed": "not_started",
                "reason": "no service has ever been started",
            }
        )
        return EXIT_OK
    observed = record.get("observed", "unknown")
    _emit(
        {
            "schema": STATUS_SCHEMA,
            "status": str(observed).upper(),
            "desired": record.get("desired"),
            "observed": observed,
            "updated_at": record.get("updated_at"),
        }
    )
    return EXIT_OK


def _not_implemented(command: str) -> int:
    return _unavailable(
        command,
        "NOT_IMPLEMENTED",
        f"'{command}' is implemented by the task-orchestration lane and is not "
        f"available in this build; no work is started or stopped here",
    )


def _cmd_run(args: argparse.Namespace) -> int:
    return _not_implemented("run")


def _cmd_start(args: argparse.Namespace) -> int:
    return _not_implemented("start")


def _cmd_stop(args: argparse.Namespace) -> int:
    return _not_implemented("stop")


def _cmd_logs(args: argparse.Namespace) -> int:
    return _not_implemented("logs")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cao",
        description=(
            "CAO source install: managed runtime bootstrap, readiness and "
            "service status. Commands not owned by this build report explicit "
            "UNAVAILABLE."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON (all output is already JSON)",
    )
    subcommands = parser.add_subparsers(dest="command", metavar="COMMAND")

    doctor = subcommands.add_parser("doctor", help="report readiness evidence")
    doctor.set_defaults(handler=_doctor)

    status = subcommands.add_parser("status", help="report the service status")
    status.set_defaults(handler=_cmd_status)

    for name, help_text, handler in (
        ("run", "submit a task (unavailable in this build)", _cmd_run),
        ("start", "start the service (unavailable in this build)", _cmd_start),
        ("stop", "stop the service (unavailable in this build)", _cmd_stop),
        ("logs", "read service logs (unavailable in this build)", _cmd_logs),
    ):
        sub = subcommands.add_parser(name, help=help_text)
        sub.add_argument(
            "args",
            nargs=argparse.REMAINDER,
            help="accepted and reported as unavailable; nothing is started",
        )
        sub.set_defaults(handler=handler)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] == "--json":
        args_list = args_list[1:]
    # The task-running commands accept arbitrary arguments and then report an
    # explicit UNAVAILABLE; they are intercepted so a task argument that looks
    # like an option is never a usage error.
    if args_list and args_list[0] in {"run", "start", "stop", "logs"}:
        return _not_implemented(args_list[0])

    parser = build_parser()
    args = parser.parse_args(args_list)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return EXIT_OK
    return handler(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
