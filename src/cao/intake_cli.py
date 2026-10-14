"""The ``cao intake`` command group: durable admission, no execution.

``submit`` admits one bounded submission file and acknowledges its durable id;
``list`` reports public-safe metadata and never a body; ``show`` reads one full
record from the operator's own private state. ``pull`` is the inbound path: it
admits one submission delivered over a registered transport, reading the
document from a file or stdin. The only registered transport is the marketplace
transport (:mod:`cao.intake_bridge`); any other named transport fails closed
with structured UNAVAILABLE, and ``pull`` with no transport named reports
``NO_INBOUND_TRANSPORT`` rather than inventing a default.

``triage`` is the on-demand B3 advisory path: it reads one admitted submission
and bounded read-only evidence, and returns one advisory disposition with cited
provenance. It starts nothing, claims nothing and sends nothing; with no
qualified provider it always yields the deterministic context packet.

``status`` is the B5 request-to-resolution projection: it derives a truthful
requester status and an accountable next mover from existing evidence, read-only
and idempotent. ``--guest-view`` is refused ``UNAVAILABLE`` in this build: a
guest projection is only defensible for a host that has independently
authenticated the caller, and no such host connector exists, so the CLI never
trusts a principal or scope asserted through process environment or CLI input.
``reply`` is the B5 outbound gate: a real send is never available, so it serves
only a deterministic ``--dry-run`` or an explicit ``--mock`` run of the built-in
no-egress simulation, and only after a separate operator authorizing event.

Output is JSON on stdout. Exit codes are ``0`` success, ``2`` usage error and
``3`` explicit UNAVAILABLE/conflict, matching the rest of CAO's CLI.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from . import (intake, intake_bridge, intake_compile, intake_handoff,
               intake_resolution, intake_triage)
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


def _refusal(command: str, code: str, message: str, status: str,
             reasons: list[str] | None = None) -> int:
    payload: dict[str, Any] = {
        "schema": ERROR_SCHEMA,
        "status": status,
        "command": command,
        "error": {"code": code, "verdict": status, "message": message},
    }
    if reasons:
        payload["error"]["reasons"] = reasons
    _emit(payload)
    return EXIT_UNAVAILABLE


def _cmd_compile(args: argparse.Namespace) -> int:
    """Compile one PROPOSE_WORK packet into a reviewable draft (no dispatch).

    Every input is validated before anything is written; ``--dry-run`` returns
    the would-be draft and creates no state at all. No team is initialized and
    no work is started, claimed or dispatched.
    """

    try:
        result = intake_compile.compile_route(
            args.packet_id,
            args.route_file,
            dry_run=bool(getattr(args, "dry_run", False)),
        )
    except intake_compile.CompileNeedsReview as exc:
        return _refusal("compile", exc.code, str(exc), "NEEDS_HUMAN_REVIEW",
                        exc.reasons)
    except (intake.IntakeError, intake_triage.TriageError,
            intake_compile.CompileError) as exc:
        return _error("compile", exc.code, str(exc))
    except (StateError, OSError, UnicodeError, ValueError) as exc:
        return _error("compile", "ROUTE_ERROR", type(exc).__name__)
    _emit(result)
    return EXIT_OK


def _cmd_triage(args: argparse.Namespace) -> int:
    """On-demand advisory triage of one admitted submission (read-only).

    Every input is validated before anything is read: refs are checked as Git
    refs, the cutoff as an explicit UTC timestamp and ``--implemented`` as a
    commit id, so a malformed option never reaches the repository or the store.
    """

    submission_id = getattr(args, "id", None)
    item_id = getattr(args, "item_id", None)
    try:
        if submission_id is not None and item_id is not None:
            derived = intake.submission_id_for(item_id)
            if derived != submission_id:
                return _error(
                    "triage", "USAGE",
                    "--id and --item-id name different items",
                )
        elif item_id is not None:
            submission_id = intake.submission_id_for(item_id)
        elif submission_id is None:
            return _error("triage", "USAGE", "triage requires --id or --item-id")
        result = intake_triage.triage(
            submission_id,
            repo=getattr(args, "repo", None),
            source_ref=getattr(args, "source_ref", None),
            cutoff=getattr(args, "cutoff", None),
            implemented=getattr(args, "implemented", None),
        )
    except (intake.IntakeError, intake_triage.TriageError) as exc:
        return _error("triage", exc.code, str(exc))
    except (StateError, OSError, UnicodeError, ValueError) as exc:
        return _error("triage", "TRIAGE_ERROR", type(exc).__name__)
    _emit(result)
    return EXIT_OK


def _cmd_status(args: argparse.Namespace) -> int:
    """Derive one truthful request-to-resolution status (read-only)."""

    submission_id = getattr(args, "id", None)
    item_id = getattr(args, "item_id", None)
    try:
        if submission_id is not None and item_id is not None:
            derived = intake.submission_id_for(item_id)
            if derived != submission_id:
                return _error(
                    "status", "USAGE", "--id and --item-id name different items"
                )
        elif item_id is not None:
            submission_id = intake.submission_id_for(item_id)
        elif submission_id is None:
            return _error("status", "USAGE", "status requires --id or --item-id")
        if getattr(args, "guest_view", False):
            # Fail closed. A guest projection is only defensible for a caller a
            # host has *independently authenticated*. Process environment and
            # CLI arguments are caller-controlled, so this build serves no guest
            # view from them: a forged host identity or scope, or any submission
            # body, is not proof and yields UNAVAILABLE.
            return _error(
                "status",
                "RESOLUTION_UNAVAILABLE",
                "the guest view is UNAVAILABLE: it requires an independently "
                "authenticated host connector, and this build serves no guest "
                "projection from process environment or CLI input",
            )
        document = intake_resolution.derive_status(submission_id)
    except intake_resolution.ResolutionRefused as exc:
        return _refusal("status", exc.code, str(exc), "REFUSED")
    except (intake.IntakeError, intake_resolution.ResolutionError) as exc:
        return _error("status", exc.code, str(exc))
    except (StateError, OSError, UnicodeError, ValueError) as exc:
        return _error("status", "RESOLUTION_ERROR", type(exc).__name__)
    _emit(document)
    return EXIT_OK


def _cmd_reply(args: argparse.Namespace) -> int:
    """Simulate one gated origin-channel reply (no egress)."""

    try:
        result = intake_resolution.reply(
            args.id,
            kind=args.kind,
            auth_file=args.auth_file,
            dry_run=bool(getattr(args, "dry_run", False)),
            mock=bool(getattr(args, "mock", False)),
        )
    except intake_resolution.ResolutionRefused as exc:
        return _refusal("reply", exc.code, str(exc), "REFUSED")
    except (intake.IntakeError, intake_resolution.ResolutionError) as exc:
        return _error("reply", exc.code, str(exc))
    except (StateError, OSError, UnicodeError, ValueError) as exc:
        return _error("reply", "RESOLUTION_ERROR", type(exc).__name__)
    _emit(result)
    return EXIT_OK


def _cmd_handoff(args: argparse.Namespace) -> int:
    """Derive one governed Intake-to-CAO READ handoff plan (no dispatch).

    The plan is safe by construction: it is derived from the original admission
    and its B3 advisory, it starts nothing, and it is executed only through an
    in-process authenticated operator authority. The CLI never constructs that
    authority and never accepts one from a flag, the environment or a file, so
    ``--authorize`` is an honest structured ``UNAVAILABLE`` rather than a
    caller-asserted grant. ``--dry-run`` creates no state at all.
    """

    submission_id = getattr(args, "id", None)
    item_id = getattr(args, "item_id", None)
    try:
        if submission_id is not None and item_id is not None:
            derived = intake.submission_id_for(item_id)
            if derived != submission_id:
                return _error("handoff", "USAGE", "--id and --item-id name different items")
        elif item_id is not None:
            submission_id = intake.submission_id_for(item_id)
        elif submission_id is None:
            return _error("handoff", "USAGE", "handoff requires --id or --item-id")
        if getattr(args, "authorize", False) and getattr(args, "inspect", False):
            return _error("handoff", "USAGE",
                          "choose either --inspect or --authorize, not both")
        if getattr(args, "inspect", False):
            document = intake_handoff.handoff_status(submission_id)
            _emit(document)
            return EXIT_OK
        if getattr(args, "authorize", False):
            # Fail closed. The governed authorize route needs a host that has
            # *independently authenticated* the operator and granted this exact
            # scoped READ execution in process. A flag, an environment variable
            # or a JSON file is caller-controlled and is never proof, so this
            # build serves no CLI authorize route and creates nothing.
            return _error(
                "handoff",
                "HANDOFF_UNAVAILABLE",
                "the governed authorize route is UNAVAILABLE: it requires an "
                "in-process authenticated operator authority, and a CLI flag, "
                "an environment variable or a JSON file is caller-controlled "
                "and is never proof; supply the in-process adapter through the "
                "library 'operator_authority' argument",
            )
        result = intake_handoff.handoff_plan(
            submission_id,
            repo=getattr(args, "repo", None),
            source_ref=getattr(args, "source_ref", None),
            operator_authority=None,
            dry_run=bool(getattr(args, "dry_run", False)),
        )
    except intake_handoff.HandoffRefused as exc:
        return _refusal("handoff", exc.code, str(exc), "REFUSED")
    except (intake.IntakeError, intake_triage.TriageError,
            intake_handoff.HandoffError) as exc:
        return _error("handoff", exc.code, str(exc))
    except (StateError, OSError, UnicodeError, ValueError) as exc:
        return _error("handoff", "HANDOFF_ERROR", type(exc).__name__)
    _emit(result)
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

    triage = commands.add_parser(
        "triage",
        help="on-demand advisory triage of one admitted submission (read-only)",
    )
    triage.add_argument("--id", metavar="SUBMISSION_ID",
                        help="durable submission id to triage")
    triage.add_argument("--item-id", metavar="ITEM_ID",
                        help="stable item id; its submission id is derived")
    triage.add_argument("--repo", metavar="PATH",
                        help="repository for bounded read-only Git evidence")
    triage.add_argument("--source-ref", metavar="REF",
                        help="ref or commit id to read as source evidence")
    triage.add_argument("--cutoff", metavar="ISO8601",
                        help="evidence cutoff (YYYY-MM-DDThh:mm:ssZ or YYYY-MM-DD, UTC)")
    triage.add_argument("--implemented", metavar="COMMIT",
                        help="commit id that already implements the request")
    triage.set_defaults(handler=_cmd_triage)

    compile_cmd = commands.add_parser(
        "compile",
        help="compile a PROPOSE_WORK packet into a reviewable draft (no dispatch)",
    )
    compile_cmd.add_argument("--packet-id", required=True, metavar="PACKET_ID",
                             help="advisory triage packet dispositioned PROPOSE_WORK")
    compile_cmd.add_argument("--route-file", required=True, metavar="PATH",
                             help="bounded operator-supplied route evidence JSON")
    compile_cmd.add_argument("--dry-run", action="store_true",
                             help="return the draft and create no state")
    compile_cmd.set_defaults(handler=_cmd_compile)

    status_cmd = commands.add_parser(
        "status",
        help="truthful request-to-resolution status projection (read-only)",
    )
    status_cmd.add_argument("--id", metavar="SUBMISSION_ID",
                            help="durable submission id to project")
    status_cmd.add_argument("--item-id", metavar="ITEM_ID",
                            help="stable item id; its submission id is derived")
    status_cmd.add_argument("--guest-view", action="store_true",
                            help="request a redacted guest projection; "
                                 "UNAVAILABLE in this build without an "
                                 "authenticated host connector")
    status_cmd.set_defaults(handler=_cmd_status)

    reply_cmd = commands.add_parser(
        "reply",
        help="simulate one gated origin-channel reply (no egress)",
    )
    reply_cmd.add_argument("--id", required=True, metavar="SUBMISSION_ID",
                           help="durable submission id to reply about")
    reply_cmd.add_argument("--kind", required=True, choices=intake_resolution.SEND_KINDS,
                           help="followup, answer or event receipt")
    reply_cmd.add_argument("--auth-file", required=True, metavar="PATH",
                           help="separate operator authorizing event JSON")
    reply_cmd.add_argument("--dry-run", action="store_true",
                           help="deterministic no-egress plan; sends nothing")
    reply_cmd.add_argument("--mock", action="store_true",
                           help="built-in simulated adapter; sends nothing")
    reply_cmd.set_defaults(handler=_cmd_reply)

    handoff_cmd = commands.add_parser(
        "handoff",
        help="governed Intake-to-CAO READ handoff plan (no dispatch)",
    )
    handoff_cmd.add_argument("--id", metavar="SUBMISSION_ID",
                             help="durable submission id to hand off")
    handoff_cmd.add_argument("--item-id", metavar="ITEM_ID",
                             help="stable item id; its submission id is derived")
    handoff_cmd.add_argument("--repo", metavar="PATH",
                             help="scoped checkout whose HEAD the plan binds")
    handoff_cmd.add_argument("--source-ref", metavar="REF",
                             help="ref or commit id to bind as the source")
    handoff_cmd.add_argument("--dry-run", action="store_true",
                             help="derive the plan and create no state")
    handoff_cmd.add_argument("--inspect", action="store_true",
                             help="derive the read-only handoff status instead")
    handoff_cmd.add_argument("--authorize", action="store_true",
                             help="attempt the governed authorize route; "
                                  "UNAVAILABLE in this build (no in-process "
                                  "authenticated operator authority exists)")
    handoff_cmd.set_defaults(handler=_cmd_handoff)
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
