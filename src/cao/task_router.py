"""The one discoverable "send to CAO" request entrypoint: a deterministic router.

A caller sends exactly one bounded request document. This module classifies it
deterministically and routes it to exactly one *existing* governed path:

* an ordinary request is admitted through the existing Intake gate, which
  persists one durable record and starts nothing; or
* an already-shaped ``cao.team.v1`` manifest is routed to the existing CAO Team
  Execution contract, reported read-only with the exact governed next step.

The router never dispatches a team, starts an agent, grants authority, bypasses
the Work Registry, upgrades privilege, deletes anything, auto-merges, pushes,
deploys or creates a second store. An Intake admission is **never** execution
consent: the two routes are disjoint, and an execution route is qualified only
through the existing Team Execution authorization (the operator's model
allowlist and the team approval policy). A document that cannot be classified
with certainty is refused, never guessed.

This module is the routing contract, not an orchestrator: the execution route
records no approval and performs no effect, because only the operator's own
governed ``cao team init``/``cao team start`` decisions authorize a team.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from . import intake, product_config, team, team_approval

__all__ = ["main", "build_parser", "classify", "route", "SCHEMA",
           "ROUTE_INTAKE", "ROUTE_EXECUTION", "STATUS_ROUTED", "STATUS_REFUSED",
           "EXIT_OK", "EXIT_USAGE", "EXIT_UNAVAILABLE"]

SCHEMA = "cao.request.v1"

ROUTE_INTAKE = "INTAKE"
ROUTE_EXECUTION = "EXECUTION"

STATUS_ROUTED = "ROUTED"
STATUS_REFUSED = "REFUSED"

#: The largest request document this entrypoint will read. A team manifest may
#: carry up to MAX_TASKS long prompts, so it is bounded above an Intake
#: submission but still finite; the bound is fixed, never derived from input.
MAX_REQUEST_BYTES = 1024 * 1024

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3

_ROUTED_OUTCOMES = frozenset({"ADMITTED", "DUPLICATE", STATUS_ROUTED})


def _emit(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False))


def _refusal(code: str, message: str, *,
             status: str = STATUS_REFUSED) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": status,
        "route": None,
        "authority": "none",
        "dispatch": False,
        "error": {"code": code, "verdict": status, "message": message},
    }


def classify(document: Any) -> str | None:
    """The single route for a request document, or ``None`` when unroutable.

    The decision depends only on the document's own declared shape, never on a
    caller flag, environment value or prior admission, so the same request is
    always routed the same way.
    """

    if not isinstance(document, Mapping):
        return None
    schema = document.get("schema")
    if schema == team.SPEC_SCHEMA:
        return ROUTE_EXECUTION
    if schema == intake.REQUEST_SCHEMA:
        return ROUTE_INTAKE
    if schema is None and "item_id" in document and "body" in document:
        # A plain ordinary request that did not name the Intake schema is still
        # unambiguously an admission: it carries the two required fields and no
        # manifest identity.
        return ROUTE_INTAKE
    return None


def _route_intake(document: Mapping[str, Any], *, env: Mapping[str, str] | None,
                  dry_run: bool, clock: Any) -> dict[str, Any]:
    """Admit one ordinary request through the existing Intake gate.

    A plain request that omitted the Intake schema is completed from the
    document's own ``item_id``/``body`` (the exact fields :func:`classify`
    required) before the existing gate validates it; no identity is invented and
    no other field is added.
    """

    if document.get("schema") is None:
        document = {**document, "schema": intake.REQUEST_SCHEMA}
    if dry_run:
        record = intake.normalize_submission(document, clock=clock)
        return {
            "schema": SCHEMA, "status": STATUS_ROUTED, "route": ROUTE_INTAKE,
            "authority": "none", "dispatch": False, "dry_run": True,
            "source": "intake", "submission": intake.public_view(record),
        }
    admission = intake.submit(document, env=env, clock=clock)
    return {
        "schema": SCHEMA, "status": admission["status"], "route": ROUTE_INTAKE,
        "authority": "none", "dispatch": False, "dry_run": False,
        "source": "intake", "duplicate": admission["duplicate"],
        "submission": admission["submission"],
    }


def _route_execution(spec: Any, *, config: Any,
                     source: str | None) -> dict[str, Any]:
    """Report the existing governed Team Execution route for one manifest.

    The manifest is structurally validated and each resolved work/review route
    is independently qualified against the existing Team Execution
    authorization (the operator's model allowlist and approval policy). The
    route records no approval and performs no effect: it returns the exact
    governed next commands the accountable operator must run.
    """

    try:
        selected = team.validate_spec(spec)
    except team.TeamError as exc:
        return _refusal(getattr(exc, "code", None) or "TEAM_INVALID", str(exc))
    resolved = config if config is not None else product_config.load_config()
    if resolved is None:
        return _refusal(
            "PRODUCT_CONFIG_UNAVAILABLE",
            "no local CAO product configuration is installed, so the team's "
            "resolved routes cannot be independently qualified; run setup.sh "
            "and 'cao doctor' first",
            status="UNAVAILABLE",
        )
    try:
        bound = team._dispatchable_routes(resolved, selected)
    except (team.TeamError, product_config.ProductConfigError) as exc:
        return _refusal(getattr(exc, "code", None) or "TEAM_INVALID", str(exc))
    denied = sorted(
        f"{task_id}/{phase}"
        for task_id, phases in bound["allowed"].items()
        for phase, permitted in phases.items() if not permitted
    )
    if denied:
        return _refusal(
            "TEAM_MODEL_NOT_ALLOWED",
            "resolved routes are not independently authorized by the existing "
            "Team Execution policy: " + ", ".join(denied),
        )
    try:
        mode = team_approval.policy(resolved)["mode"]
    except team_approval.ApprovalError as exc:
        return _refusal(getattr(exc, "code", None) or "TEAM_INVALID", str(exc))
    reference = source or "<manifest.json>"
    next_commands = [f"cao team plan --file {reference}"]
    if mode == "chat":
        next_commands.append(f"cao team init --file {reference} --approved")
    else:
        next_commands.append(f"cao team init --file {reference}")
    next_commands.append(f"cao team start --id {selected['team_id']}")
    return {
        "schema": SCHEMA, "status": STATUS_ROUTED, "route": ROUTE_EXECUTION,
        "authority": "none", "dispatch": False, "dry_run": False,
        "source": "team_execution",
        "team_id": selected["team_id"],
        "initiative": selected.get("initiative"),
        "spec_sha256": team.fingerprint(selected),
        "authorization": {
            "authority_source": "team_execution",
            "approval_mode": mode,
            "independent_authorization_required": mode == "chat",
        },
        "next_commands": next_commands,
    }


def route(document: Any, *, env: Mapping[str, str] | None = None,
          config: Any = None, source: str | None = None,
          dry_run: bool = False, clock: Any = None) -> dict[str, Any]:
    """Route one request document deterministically and fail closed.

    The two routes are disjoint: an Intake request is only ever admitted, a
    ``cao.team.v1`` manifest is only ever reported to the governed Team
    Execution path. Neither route starts work, so no caller can obtain a first
    WRITE dispatch, a grant or a Registry effect from this entrypoint.
    """

    kind = classify(document)
    if kind == ROUTE_INTAKE:
        try:
            return _route_intake(document, env=env, dry_run=dry_run, clock=clock)
        except intake.IntakeUnavailable as exc:
            return _refusal(getattr(exc, "code", None) or "INTAKE_UNAVAILABLE",
                            str(exc), status="UNAVAILABLE")
        except intake.IntakeError as exc:
            return _refusal(getattr(exc, "code", None) or "INTAKE_INVALID", str(exc))
    if kind == ROUTE_EXECUTION:
        return _route_execution(document, config=config, source=source)
    return _refusal(
        "UNKNOWN_REQUEST",
        "the request is neither a valid Intake request nor a cao.team.v1 "
        "manifest, so it cannot be routed without guessing",
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("request has a duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_token: str) -> Any:
    raise ValueError("request carries a non-finite JSON number")


def _load_document(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read one bounded request document with strict, fail-closed parsing.

    The final path component is opened ``O_NOFOLLOW`` and ``fstat``-checked as a
    regular file, so a symlink or a device is refused. Duplicate JSON keys and
    non-finite numbers are refused, and the document must be a JSON object.
    """

    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0))
    fd = os.open(os.fspath(path), flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("request input must be a regular file")
        if info.st_size > MAX_REQUEST_BYTES:
            raise ValueError(f"request exceeds {MAX_REQUEST_BYTES} bytes")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            raw = stream.read(MAX_REQUEST_BYTES + 1)
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError(f"request exceeds {MAX_REQUEST_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("request is not valid UTF-8") from exc
    try:
        document = json.loads(text, object_pairs_hook=_unique_object,
                              parse_constant=_reject_constant)
    except ValueError as exc:
        raise ValueError("request is not valid strict JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("request must be a JSON object")
    return document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cao request",
        description=(
            "Send one request to CAO. It routes deterministically to Intake or to "
            "the existing governed Team Execution path; it dispatches nothing."
        ),
    )
    parser.add_argument("--file", metavar="PATH",
                        help="one JSON request document (Intake request or team manifest)")
    parser.add_argument("--item-id", metavar="ID",
                        help="stable item id for a plain-text request")
    parser.add_argument("--text", metavar="TEXT",
                        help="plain-text request body")
    parser.add_argument("--dry-run", action="store_true",
                        help="classify and validate; admit nothing")
    return parser


def _error(code: str, message: str) -> int:
    _emit({"schema": SCHEMA, "status": "UNAVAILABLE", "command": "request",
           "error": {"code": code, "verdict": "UNAVAILABLE", "message": message}})
    return EXIT_UNAVAILABLE


def main(argv: Sequence[str] | None = None) -> int:
    args_list = [arg for arg in (argv or ()) if arg != "--json"]
    parser = build_parser()
    try:
        args = parser.parse_args(args_list)
    except SystemExit as exc:
        return int(exc.code)
    if args.file and (args.item_id or args.text):
        return _error("USAGE", "--file cannot be combined with --item-id/--text")
    if args.file:
        try:
            document = _load_document(args.file)
        except (OSError, UnicodeError, ValueError) as exc:
            return _error("REQUEST_UNREADABLE", f"{type(exc).__name__}: {exc}")
        source = args.file
    elif args.item_id and args.text:
        document = {"schema": intake.REQUEST_SCHEMA,
                    "item_id": args.item_id, "body": args.text}
        source = None
    else:
        return _error("USAGE", "request requires --file, or --item-id and --text")
    result = route(document, source=source, dry_run=bool(args.dry_run))
    _emit(result)
    return EXIT_OK if result.get("status") in _ROUTED_OUTCOMES else EXIT_UNAVAILABLE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
