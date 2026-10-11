"""Read-only Evidence-to-Investigation CLI. No provider I/O or mutable authority."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import improvement

MAX_INPUT_BYTES = 512 * 1024
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024


def _reject_constant(token: str) -> Any:
    raise ValueError("nonfinite JSON number")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key[:80])
        result[key] = value
    return result


def _open_bounded_regular(path: str, bound: int):
    # Single open + fstat: no check-then-open gap for the final path component.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > bound:
            raise improvement.ImprovementError("input is not a bounded regular file")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _load(path: str) -> Any:
    with _open_bounded_regular(path, MAX_INPUT_BYTES) as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise improvement.ImprovementError("input exceeds 512 KiB bound")
    return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant,
                      object_pairs_hook=_unique_pairs)


def _artifact_digest(path: str) -> str:
    digest = hashlib.sha256()
    consumed = 0
    with _open_bounded_regular(path, MAX_ARTIFACT_BYTES) as stream:
        while True:
            chunk = stream.read(128 * 1024)
            if not chunk:
                break
            consumed += len(chunk)
            if consumed > MAX_ARTIFACT_BYTES:
                raise improvement.ImprovementError("artifact exceeds 16 MiB bound")
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _check_proof(case: dict, after: dict | None, ref: str, artifact: str) -> dict:
    case = improvement.validate_case(case)
    proofs = [sig["proof"] for sig in case["signals"]] + [
        goal["before"]["proof"] for goal in case["objectives"]]
    if after is not None:
        after = improvement.validate_after(after, case)
        proofs += [after["intervention"][stage]["proof"]
                   for stage in ("review", "ci", "deployment")]
        proofs += [item["measurement"]["proof"] for item in after["measurements"]]
    matches = [proof for proof in proofs if proof["ref"] == ref]
    if not matches:
        raise improvement.ImprovementError("no such evidence reference in this case")
    expected = {proof["fingerprint"] for proof in matches}
    if len(expected) != 1:
        raise improvement.ImprovementError("conflicting hashes for the same evidence reference")
    actual = _artifact_digest(artifact)
    verdicts = {p["verdict"] for p in matches}
    sources = {p["source"] for p in matches}
    if len(verdicts) != 1 or len(sources) != 1:
        raise improvement.ImprovementError("conflicting source status for one proof")
    declared_verdict = next(iter(verdicts))
    result_status = (
        "MISMATCH" if actual not in expected else
        "MATCH" if declared_verdict == "SUPPORTED" else "UNQUALIFIED"
    )
    return {
        "schema": "cao.improvement.artifact-check.v1",
        "status": result_status, "source": next(iter(sources)),
        "source_verdict": declared_verdict,
        "ref": ref, "expected_fingerprint": next(iter(expected)),
        "computed_fingerprint": actual,
        "case_fingerprint": improvement.evidence.fingerprint(case),
        "external_provenance_authenticated": False,
        "warning": "Selected bytes match or differ from the claimed digest; original producer remains unverified.",
    }


def _emit(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cao improve", description="Read-only evidence-driven improvement drafts")
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("assess", "context", "propose", "verify"):
        cmd = subs.add_parser(name)
        cmd.add_argument("--file", required=True, help="qualified improvement case JSON")
        if name == "context":
            cmd.add_argument("--resource", required=True)
            cmd.add_argument("--depth", type=int, default=1)
        if name == "propose":
            cmd.add_argument("--cause", required=True,
                             help="operator hypothesis and bounded scope JSON")
        if name == "verify":
            cmd.add_argument("--after", required=True,
                             help="attested post-change measurements JSON")
    cmd = subs.add_parser("learn")
    cmd.add_argument("--history", required=True,
                     help="list of source-attested case and outcome pairs")
    check = subs.add_parser("check-proof", help="compare artifact bytes with source proof")
    check.add_argument("--file", required=True)
    check.add_argument("--after", help="also search post-change stage and metric proofs")
    check.add_argument("--ref", required=True)
    check.add_argument("--artifact", required=True, help="operator-selected local artifact")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    try:
        if args.command == "learn":
            result = improvement.learn(_load(args.history))
        else:
            case = _load(args.file)
            if args.command == "assess":
                result = improvement.assess(case)
            elif args.command == "check-proof":
                result = _check_proof(case, _load(args.after) if args.after else None,
                                      args.ref, args.artifact)
            elif args.command == "context":
                result = improvement.context_for(case, args.resource, depth=args.depth)
            elif args.command == "propose":
                result = improvement.propose(case, _load(args.cause))
            else:
                result = improvement.verify(case, _load(args.after))
    except (improvement.ImprovementError, OSError, UnicodeError, ValueError,
            RecursionError, TypeError, KeyError, StopIteration) as exc:
        _emit({"schema": "cao.improvement.error.v1", "status": "UNAVAILABLE",
               "code": getattr(exc, "code", type(exc).__name__)})
        return 3
    _emit(result)
    if args.command == "check-proof" and result["status"] != "MATCH":
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
