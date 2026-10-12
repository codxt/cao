"""The product-facing CAO command line for a source install.

``./cao`` dispatches here, and so does the installed ``cao`` console script, so
an installed CAO never offers a different command surface from a checkout. The
read-only foundation groups (``plugins``, ``work``, ``worker``, ``run list``,
``coordinator``, ``relay``, ``automation``) are answered by the module that owns
them instead of becoming a usage error.

The commands the source bootstrap owns answer today:

``doctor``
    Exact per-subsystem readiness evidence. Fail-closed: anything that cannot be
    positively established stays ``UNKNOWN``/``UNAVAILABLE`` and the verdict is
    ``NOT_READY`` with a nonzero exit.

``status``
    The durable service record merged with the latest task record. On a fresh
    clone no record exists, so the service is honestly reported
    ``NOT_STARTED``; no running service is ever invented.

``run`` / ``stop`` / ``logs``
    The task lifecycle, owned here. ``run`` claims authority in the Work
    Registry (READ for a review, WRITE only when the user declares
    ``--write``), provisions a CAO-owned workspace/pane, starts the configured
    agent kind and submits the prompt exactly once. ``stop`` releases the claim
    and closes only CAO's own pane; ``logs`` shows the bounded captured output.
    With no product configuration installed these report explicit
    ``UNAVAILABLE``; no work is ever faked.

Output is structured JSON on stdout. Exit codes are ``0`` success, ``2`` usage
error, and ``3`` explicit ``UNAVAILABLE``/``NOT_READY``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from . import (
    bootstrap,
    product_config,
    product_daemon,
    product_driver,
    product_state,
    product_task,
)
from .state import StateError

__all__ = ["main", "build_parser", "EXIT_OK", "EXIT_USAGE", "EXIT_UNAVAILABLE"]

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3

STATUS_SCHEMA = "cao.product.status.v0"
UNAVAILABLE_SCHEMA = "cao.product.unavailable.v0"

#: The command groups this product CLI answers itself. Anything else is owned by
#: the read-only foundation CLI, which also owns ``run list`` ledger inspection.
_PRODUCT_GROUPS = frozenset(
    {"doctor", "status", "daemon", "team", "improve", "run", "start", "stop", "logs"}
)


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
    try:
        task = product_state.latest_task()
    except StateError as exc:
        return _unavailable(
            "status",
            "TASK_STATE_UNREADABLE",
            f"{type(exc).__name__}: {exc}",
        )
    if record is None and task is None:
        _emit(
            {
                "schema": STATUS_SCHEMA,
                "status": "NOT_STARTED",
                "desired": "stopped",
                "observed": "not_started",
                "task": None,
                "reason": "no service has ever been started and no task recorded",
            }
        )
        return EXIT_OK
    service = record or product_state.default_service_record()
    observed = service.get("observed", "unknown")
    if observed == "running":
        # A service record is only an intention/last observation. Confirm the
        # daemon's live PID identity before telling an operator it is running.
        try:
            daemon = product_daemon.status()
        except StateError as exc:
            return _unavailable(
                "status", "DAEMON_STATE_UNREADABLE", f"{type(exc).__name__}: {exc}"
            )
        if daemon.get("status") != "RUNNING":
            observed = "unknown"
    task_view = product_task.task_payload(task) if task is not None else None
    if task_view is not None:
        status = str(task_view.get("status") or "").upper() or str(observed).upper()
    else:
        status = str(observed).upper()
    _emit(
        {
            "schema": STATUS_SCHEMA,
            "status": status,
            "desired": service.get("desired"),
            "observed": observed,
            "updated_at": service.get("updated_at"),
            "task": task_view,
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


def _task_layer_unavailable(command: str) -> int:
    return _unavailable(
        command,
        "NOT_IMPLEMENTED",
        f"'{command}' needs a product configuration; none is installed here, so "
        f"no task is run, stopped, or shown",
    )


def _managed_executable() -> tuple[str | None, str | None]:
    """The installed managed runtime path, or a reason it is unavailable."""

    try:
        record = product_state.read_managed()
    except StateError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if record is None:
        return None, "the managed runtime has not been installed; run setup.sh"
    path = record.get("path")
    if not isinstance(path, str) or not path:
        return None, "the managed runtime record has no installed path"
    if not os.path.isfile(path):
        return None, "the managed runtime executable is missing on disk"
    return path, None


def _task_exit(payload: Mapping[str, Any]) -> int:
    status = str(payload.get("status") or "").lower()
    if status in ("complete", "running", "claimed", "pending"):
        return EXIT_OK
    return EXIT_UNAVAILABLE


def _run(rest: Sequence[str], config: product_config.ProductConfig) -> int:
    parser = argparse.ArgumentParser(prog="cao run", add_help=False)
    parser.add_argument("--repo")
    parser.add_argument("--id")
    parser.add_argument("--mode", choices=("read", "write"), default="read")
    parser.add_argument("--write", action="append", default=[])
    parser.add_argument("--wait-ms", type=int, default=None)
    parser.add_argument("prompt", nargs=argparse.REMAINDER)
    try:
        args = parser.parse_args(list(rest))
    except SystemExit:
        return _unavailable("run", "USAGE", "invalid 'run' arguments")
    if not args.repo:
        return _unavailable("run", "USAGE", "'run' requires --repo <path>")
    if not args.prompt:
        return _unavailable("run", "USAGE", "'run' requires a task prompt")
    prompt = " ".join(args.prompt).strip()
    executable, why = _managed_executable()
    if executable is None:
        return _unavailable("run", "MANAGED_RUNTIME_NOT_READY", why or "")
    driver = product_driver.RuntimeDriver(executable=executable)
    observation = None
    if args.write:
        # A WRITE claim is gated by the registry on a live, read-only occupancy
        # observation. Collect it only for WRITE: READ is ungated and never
        # probes the runtime. An unreadable or partial reading maps to
        # unavailable evidence, and the registry then refuses the claim.
        observation = product_daemon.to_runtime_observation(
            product_daemon.collect_occupancy(driver)
        )
    try:
        payload = product_task.run_task(
            driver=driver,
            repo=args.repo,
            prompt=prompt,
            task_id=args.id,
            mode=args.mode,
            write_scope=args.write,
            config=config,
            observation=observation,
            wait_ms=args.wait_ms,
        )
    except (
        product_task.TaskUsageError,
        product_task.TaskUnavailable,
        product_task.TaskAmbiguous,
        product_config.ProductConfigError,
    ) as exc:
        return _unavailable("run", getattr(exc, "code", "PRODUCT_TASK_ERROR"), str(exc))
    except StateError as exc:
        return _unavailable("run", "TASK_STATE_UNREADABLE", f"{type(exc).__name__}: {exc}")
    _emit(payload)
    return _task_exit(payload)


def _cmd_start(args: argparse.Namespace) -> int:
    return _not_implemented("start")


def _daemon(rest: Sequence[str]) -> int:
    """The daemon lifecycle: start / stop / status.

    ``start`` refuses (exit 3) unless the product configuration and the managed
    runtime are both ready; it never invents a running service. ``status`` and
    ``stop`` read the durable records and work without a configuration.
    """

    parser = argparse.ArgumentParser(prog="cao daemon", add_help=False)
    parser.add_argument("action", choices=("start", "stop", "status"))
    parser.add_argument("--max-ticks", type=int, default=None)
    try:
        args = parser.parse_args(list(rest))
    except SystemExit:
        return _unavailable("daemon", "USAGE", "invalid 'daemon' arguments")

    try:
        config = product_config.load_config()
    except (product_config.ProductConfigError, StateError) as exc:
        return _unavailable(
            "daemon", "PRODUCT_CONFIG_INVALID", f"{type(exc).__name__}: {exc}"
        )

    if args.action == "status":
        try:
            payload = product_daemon.status()
        except StateError as exc:
            return _unavailable(
                "daemon", "DAEMON_STATE_UNREADABLE", f"{type(exc).__name__}: {exc}"
            )
        _emit(payload)
        return EXIT_OK

    if args.action == "stop":
        try:
            payload = product_daemon.Daemon(config=config, max_ticks=args.max_ticks).stop()
        except StateError as exc:
            return _unavailable(
                "daemon", "DAEMON_STATE_UNREADABLE", f"{type(exc).__name__}: {exc}"
            )
        _emit(payload)
        return EXIT_OK

    # start
    if config is None:
        return _unavailable(
            "daemon",
            "PRODUCT_CONFIG_NOT_READY",
            "no product configuration is installed; run setup.sh first",
        )
    executable, why = _managed_executable()
    if executable is None:
        return _unavailable("daemon", "MANAGED_RUNTIME_NOT_READY", why or "")
    driver = product_driver.RuntimeDriver(executable=executable)
    daemon = product_daemon.Daemon(
        driver=driver, config=config, max_ticks=args.max_ticks
    )
    try:
        payload = daemon.start()
    except (product_daemon.DaemonError, StateError) as exc:
        return _unavailable(
            "daemon", getattr(exc, "code", "PRODUCT_DAEMON_ERROR"), str(exc)
        )
    _emit(payload)
    return EXIT_OK


def _cmd_daemon(args: argparse.Namespace) -> int:
    return _daemon(getattr(args, "args", []) or [])


def _cmd_run(args: argparse.Namespace) -> int:  # parser placeholder; main routes run
    return _not_implemented("run")


def _stop(rest: Sequence[str], config: product_config.ProductConfig) -> int:
    parser = argparse.ArgumentParser(prog="cao stop", add_help=False)
    parser.add_argument("--id")
    try:
        args = parser.parse_args(list(rest))
    except SystemExit:
        return _unavailable("stop", "USAGE", "invalid 'stop' arguments")
    executable, _why = _managed_executable()
    driver = (
        product_driver.RuntimeDriver(executable=executable) if executable else None
    )
    try:
        payload = product_task.stop_task(driver=driver, task_id=args.id)
    except (product_task.TaskUnavailable, product_task.TaskUsageError) as exc:
        return _unavailable("stop", getattr(exc, "code", "PRODUCT_TASK_ERROR"), str(exc))
    except StateError as exc:
        return _unavailable("stop", "TASK_STATE_UNREADABLE", f"{type(exc).__name__}: {exc}")
    _emit(payload)
    return EXIT_OK if payload.get("status") in ("stopped", "complete") else EXIT_UNAVAILABLE


def _cmd_stop(args: argparse.Namespace) -> int:
    return _not_implemented("stop")


def _logs(rest: Sequence[str], config: product_config.ProductConfig) -> int:
    parser = argparse.ArgumentParser(prog="cao logs", add_help=False)
    parser.add_argument("--id")
    parser.add_argument("--lines", type=int, default=None)
    try:
        args = parser.parse_args(list(rest))
    except SystemExit:
        return _unavailable("logs", "USAGE", "invalid 'logs' arguments")
    try:
        payload = product_task.read_logs(args.id, limit=args.lines)
    except product_task.TaskUsageError as exc:
        return _unavailable("logs", exc.code, str(exc))
    except StateError as exc:
        return _unavailable("logs", "TASK_STATE_UNREADABLE", f"{type(exc).__name__}: {exc}")
    _emit(payload)
    return EXIT_OK if payload.get("ok") else EXIT_UNAVAILABLE


def _cmd_logs(args: argparse.Namespace) -> int:
    return _not_implemented("logs")


def _dispatch_task_command(args_list: Sequence[str]) -> int:
    command = args_list[0]
    if command == "start":
        return _not_implemented("start")
    try:
        config = product_config.load_config()
    except (product_config.ProductConfigError, StateError) as exc:
        return _unavailable(
            command, "PRODUCT_CONFIG_INVALID", f"{type(exc).__name__}: {exc}"
        )
    if config is None:
        return _task_layer_unavailable(command)
    if command == "run":
        return _run(args_list[1:], config)
    if command == "stop":
        return _stop(args_list[1:], config)
    if command == "logs":
        return _logs(args_list[1:], config)
    return _not_implemented(command)


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

    daemon = subcommands.add_parser(
        "daemon", help="run the CAO user-level orchestration daemon"
    )
    daemon.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="daemon lifecycle: start|stop|status",
    )
    daemon.set_defaults(handler=_cmd_daemon)

    team_command = subcommands.add_parser(
        "team", help="specialist Team Execution"
    )
    team_command.add_argument("args", nargs=argparse.REMAINDER)
    team_command.set_defaults(handler=_cmd_start)  # main dispatches separately

    improvement_command = subcommands.add_parser(
        "improve", help="source-bound operating evidence assessment (read-only)"
    )
    improvement_command.add_argument("args", nargs=argparse.REMAINDER)
    improvement_command.set_defaults(handler=_cmd_start)  # main dispatches separately

    for name, help_text, handler in (
        ("run", "submit a task", _cmd_run),
        ("start", "start the service (unavailable in this build)", _cmd_start),
        ("stop", "stop the task and release its claim", _cmd_stop),
        ("logs", "read captured task logs", _cmd_logs),
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
    # The installed ``cao`` command and the source launcher ``./cao`` must be
    # the same command, so a group this product CLI does not own is answered by
    # the foundation CLI that owns it instead of becoming a usage error.
    if args_list[:1] == ["run"] and args_list[1:2] == ["list"]:
        # ``run list`` is the foundation ledger inspection command; every other
        # ``run`` form below is the product task command.
        from . import cli as foundation_cli

        return foundation_cli.main(args_list)
    if (args_list and not args_list[0].startswith("-")
            and args_list[0] not in _PRODUCT_GROUPS):
        from . import cli as foundation_cli

        return foundation_cli.main(args_list)
    # The task-running commands accept arbitrary arguments and then report an
    # explicit UNAVAILABLE; they are intercepted so a task argument that looks
    # like an option is never a usage error.
    if args_list and args_list[0] in {"run", "start", "stop", "logs"}:
        return _dispatch_task_command(args_list)
    if args_list and args_list[0] == "daemon":
        return _daemon(args_list[1:])
    if args_list and args_list[0] == "team":
        from . import team_cli
        return team_cli.main(args_list[1:])
    if args_list and args_list[0] == "improve":
        from . import improvement_cli
        return improvement_cli.main(args_list[1:])

    parser = build_parser()
    args = parser.parse_args(args_list)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return EXIT_OK
    return handler(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
