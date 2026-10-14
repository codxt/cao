"""On-demand, source-aware advisory triage for admitted submissions (B3).

B3 is the *read* half of the governed intake middle. Given one already admitted
submission (B1), it assembles a bounded context packet on demand and emits **one
advisory result** with a provenance record. It is deliberately small in effect:

* a triage result is **advisory**. It is not authority, not a work item, not an
  approval, not canonical Company State and not a Work Registry claim;
* it starts, merges, dispatches, schedules, claims, sends or deploys nothing,
  and it writes no Registry row. It only ever reads;
* it reuses intake's own strict, private reader for the submission and intake's
  validated private-store primitives for its own packet, so an advisory packet
  can never be laundered through the permissive state reader and lands only in a
  real, owned ``0700`` directory as an owned ``0600`` file;
* the submission body stays untrusted, opaque data. It is inspected only to pick
  a disposition and flag risk; it is **never persisted** in the packet and never
  executed, interpolated into a shell or treated as a directive.

Exactly five dispositions are permitted, and nothing else:

``ASK_CONTEXT``
    More context is required before any grounded position is possible.
``ANSWER_CANDIDATE``
    A grounded answer may exist, and it is explicitly **not** self-authorized.
``PROPOSE_WORK``
    The request appears to map to work that must go through B4 routing; B3 only
    proposes.
``HUMAN_REVIEW``
    A human decision is required; no automated disposition is safe.
``DENY_OR_REPLAN``
    The request cannot proceed as stated and must be denied or replanned.

Model qualification is explicit and fail-closed: this build holds no provider
credential and performs **no** provider egress, so B3 always yields the
deterministic context packet instead of pretending an AI decided. There is no
automated provider-privacy classification; a future AI path must be enabled only
behind the operator-verified provider privacy gate and must keep this
deterministic path as its fallback.

Standard library only; no third-party dependencies.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from . import git, intake, product_config, registry, team
from .evidence import canonical_json, fingerprint, utc_now
from .product_state import state_dir

__all__ = [
    "ADVISORY_SCHEMA",
    "RECORD_TYPE",
    "DISPOSITIONS",
    "MAX_PACKET_BYTES",
    "MAX_SOURCES",
    "MAX_IDENT",
    "MAX_TEXT",
    "TriageError",
    "TriageInvalid",
    "TriageUnavailable",
    "TriageStateError",
    "TriageConflict",
    "triage_dir",
    "packet_path",
    "packet_id_for",
    "build_packet",
    "triage",
    "list_packets",
    "read_packet",
    "public_view",
]

#: The schema of one persisted, provenance-linked advisory triage packet.
ADVISORY_SCHEMA = "cao.intake.triage.v1"
#: The record type that separates an advisory packet from a submission and from
#: any canonical accepted work.
RECORD_TYPE = "intake_triage_advisory"
#: The only five dispositions, in no significance order.
DISPOSITIONS = (
    "ASK_CONTEXT",
    "ANSWER_CANDIDATE",
    "PROPOSE_WORK",
    "HUMAN_REVIEW",
    "DENY_OR_REPLAN",
)

#: The largest packet this module will write or read back. The packet never
#: carries the submission body, so the bound is generous but fixed.
MAX_PACKET_BYTES = 1024 * 1024
MAX_SOURCES = 160
MAX_IDENT = 200
MAX_TEXT = 2000
_MAX_REFS = intake.MAX_EVIDENCE_REFS
_MAX_UNCERTAINTIES = 32
#: The duplicate scan reads at most this many sibling submissions, so an
#: on-demand triage stays bounded even against a very large private store.
_MAX_DUPLICATE_SCAN = 256

_ERROR_VERDICTS = frozenset({"SUPPORTED", "UNKNOWN", "UNAVAILABLE"})

_PACKET_KEYS = frozenset(
    {
        "schema",
        "record_type",
        "packet_id",
        "advisory",
        "authority",
        "created_at",
        "content_fingerprint",
        "submission",
        "inputs",
        "disposition",
        "signals",
        "provenance",
        "ai",
    }
)
_SUBMISSION_KEYS = frozenset(
    {
        "submission_id",
        "item_id",
        "fingerprint",
        "source",
        "title",
        "body_redacted",
        "evidence_refs",
    }
)
_INPUT_KEYS = frozenset(
    {"submission_fingerprint", "options", "git", "source_refs", "registry", "team"}
)
_DISPOSITION_KEYS = frozenset({"value", "basis", "confidence", "uncertainties"})
_SIGNAL_KEYS = frozenset(
    {
        "grounded",
        "question_shaped",
        "work_shaped",
        "risk_flags",
        "duplicate",
        "already_implemented",
        "priority",
        "dependencies",
        "capability_hints",
    }
)
_PROVENANCE_KEYS = frozenset(
    {"evidence_cutoff", "stale", "stale_reason", "sources", "uncertainties"}
)
_AI_KEYS = frozenset({"used", "provider", "egress", "reason"})
_SOURCE_KEYS = frozenset({"kind", "ref", "fingerprint", "verdict", "reason"})

_ID_RE = re.compile(r"[0-9a-f]{64}")


class TriageError(Exception):
    """Base class for triage failures."""

    code = "TRIAGE_ERROR"


class TriageInvalid(TriageError):
    """A triage request or a packet is malformed or unusable."""

    code = "TRIAGE_INVALID"


class TriageUnavailable(TriageError):
    """A submission or a stored packet could not be reached."""

    code = "TRIAGE_UNAVAILABLE"


class TriageStateError(TriageError):
    """Persisted triage state is present but not usable."""

    code = "TRIAGE_STATE"


class TriageConflict(TriageError):
    """An advisory packet id was reused for different content."""

    code = "TRIAGE_CONFLICT"


# ---------------------------------------------------------------------------
# Deterministic classification rules (documented, no model, no heuristics that
# consult anything outside the bounded evidence bundle)
# ---------------------------------------------------------------------------

#: Work-shaped requests use one of these verbs. A word-boundary match keeps
#: "address" from matching "add".
_WORK_SHAPED = re.compile(
    r"\b(add|adds|adding|implement|implements|implementing|fix|fixes|fixing|"
    r"refactor|refactors|refactoring|remove|removes|removing|update|updates|"
    r"updating|rename|renames|renaming|migrate|migrates|migrating|introduce|"
    r"introduces|support|supports|supporting|create|creates|creating|build|"
    r"builds|building|write|writes|writing|enable|enables|enabling|disable|"
    r"disables|upgrade|upgrades|upgrading|allow|allows|prevent|prevents|"
    r"handle|handles|support)\b",
    re.IGNORECASE,
)
#: Question-shaped requests either end in a question mark or open interrogatively.
_QUESTION_START = re.compile(
    r"(what|why|how|when|where|who|which|is|are|was|were|can|could|does|do|"
    r"did|should|would|will|may|must)\b",
    re.IGNORECASE,
)
#: Explicit priority markers. Absent a match, priority stays UNKNOWN rather than
#: being guessed.
_PRIORITY_MARKER = re.compile(
    r"\b(p0|p1|critical|blocker|blocking|urgent|asap|high priority|sev[ -]?1)\b",
    re.IGNORECASE,
)

#: Risk categories that force HUMAN_REVIEW. They are *flags* only: a flagged body
#: changes no behaviour beyond the disposition, and is never treated as a
#: directive.
_RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "directive_text",
        re.compile(
            r"\b(ignore (all |the )?previous|disregard (all|previous|the)|"
            r"system prompt|you must|execute the following|run the following|"
            r"as an ai)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "shell_payload",
        re.compile(
            r"`|\$\(|\$\{|&&|\|\||>\s*/|<\(|;\s*(rm|curl|wget|sh|bash|eval|"
            r"python|sudo)\b"
        ),
    ),
    (
        "reserved_action",
        re.compile(
            r"\b(push to (github|origin|remote)|deploy|release|publish|"
            r"credential|secret|api[ _-]?key|sudo|chmod|rm -rf)\b",
            re.IGNORECASE,
        ),
    ),
)

_NOTE_NOT_AI = (
    "the disposition is derived from deterministic text and evidence rules, not "
    "from a qualified AI"
)
_NOTE_ADVISORY = (
    "priority, dependencies and required Capability are advisory and may be "
    "UNKNOWN"
)
_NO_EGRESS_REASON = (
    "no provider privacy qualification is configured for this build; the "
    "deterministic context packet is used and nothing is sent to any provider"
)


# ---------------------------------------------------------------------------
# Small validation helpers (fixed, bounded, non-echoing)
# ---------------------------------------------------------------------------


def _bounded_str(value: Any, field: str, limit: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise TriageInvalid(f"{field} must be a string")
    if not allow_empty and not value:
        raise TriageInvalid(f"{field} must be a non-empty string")
    if len(value) > limit:
        raise TriageInvalid(f"{field} exceeds {limit} characters")
    return value


def _record_id(value: Any) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise TriageInvalid("packet id must be 64 lowercase hex characters")
    return value


def _submission_id(value: Any) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise TriageInvalid("submission id must be 64 lowercase hex characters")
    return value


def _iso_from_epoch(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_cutoff(text: Any) -> tuple[str, int]:
    """Parse an operator evidence cutoff into ``(rfc3339, epoch)``.

    Only two explicit, unambiguous UTC forms are accepted; anything else is
    refused rather than guessed, so a cutoff can never be silently reinterpreted.
    """

    if not isinstance(text, str) or not text:
        raise TriageInvalid("cutoff must be a non-empty ISO-8601 UTC timestamp")
    for form in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            moment = datetime.strptime(text, form).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ"), int(moment.timestamp())
    raise TriageInvalid(
        "cutoff must be YYYY-MM-DDThh:mm:ssZ or YYYY-MM-DD in UTC"
    )


# ---------------------------------------------------------------------------
# Read-only Git evidence (bounded, allowlisted, never a shell)
# ---------------------------------------------------------------------------

#: The ONE Git command shape this module may run. It reads a commit id and its
#: committer time for an already-validated commit id or short ref. There is no
#: prefix allowlist and no user-supplied flag: an argv that is not exactly this
#: shape cannot reach git.
_GIT_SHOW_ARGV = ("show", "-s", "--format=%H %ct")
_STAMP_RE = re.compile(r"[0-9]{1,12}")


def _allowed_git_argv(args: Any) -> bool:
    if type(args) is not list or len(args) != 4:
        return False
    if tuple(args[:3]) != _GIT_SHOW_ARGV:
        return False
    operand = args[3]
    if type(operand) is not str:
        return False
    return git.valid_commit_id(operand) or git.valid_short_ref(operand)


def _run_git(repo: str, args: list[str]) -> tuple[int | None, str]:
    """Run one ALLOWLISTED read-only git observation. Never raises.

    ``shell`` is never used, the argv is validated against a complete shape, and
    optional locks are disabled so asking never rewrites the repository.
    """

    if not _allowed_git_argv(args):
        return None, ""
    argv = ["git", "--no-optional-locks", "-c", "diff.autoRefreshIndex=false"]
    argv.extend(args)
    # A minimal child environment. Only PATH, HOME and TMPDIR are forwarded, so
    # a caller's GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE or any other GIT_*
    # override cannot redirect this observation to an unrelated repository.
    env = {"GIT_OPTIONAL_LOCKS": "0"}
    for name in ("PATH", "HOME", "TMPDIR"):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    try:
        proc = subprocess.run(
            argv,
            cwd=repo,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except Exception:  # noqa: BLE001 - a probe never propagates
        return None, ""
    try:
        return proc.returncode, proc.stdout.decode("utf-8")
    except Exception:  # noqa: BLE001 - undecodable output is unreadable evidence
        return None, ""


def _show_commit(repo: str, ref: str) -> tuple[str, int] | None:
    if not (git.valid_commit_id(ref) or git.valid_short_ref(ref)):
        return None
    rc, out = _run_git(repo, ["show", "-s", "--format=%H %ct", ref])
    if rc != 0:
        return None
    line = out[:-1] if out.endswith("\n") else out
    if not line or "\n" in line or "\r" in line:
        return None
    fields = line.split(" ")
    if len(fields) != 2:
        return None
    commit, stamp = fields
    if not git.valid_commit_id(commit) or not _STAMP_RE.fullmatch(stamp):
        return None
    return commit, int(stamp)


def _git_evidence(
    repo: str | None, source_ref: str | None, cutoff_iso: str | None,
    cutoff_epoch: int | None,
) -> dict[str, Any]:
    """Bounded read-only Git/default-branch evidence, or explicit UNKNOWN.

    A missing or unusable repository is never an empty success: it is
    ``UNAVAILABLE`` (not offered) or ``UNKNOWN`` (could not be established).
    """

    base: dict[str, Any] = {
        "source": "git",
        "repo": None,
        "default_ref": None,
        "ref": None,
        "head": None,
        "commit_at": None,
        "commit_epoch": None,
        "cutoff": cutoff_iso,
        "stale": None,
    }
    if repo is None:
        return {**base, "verdict": "UNAVAILABLE",
                "reason": "no repository path was supplied"}
    work = os.path.realpath(os.path.expanduser(repo))
    base["repo"] = work
    if not os.path.isdir(work):
        return {**base, "verdict": "UNKNOWN",
                "reason": "the supplied repository path is not a directory"}
    trees = git.probe_worktrees(work)
    if not trees.get("ok"):
        return {**base, "verdict": "UNKNOWN",
                "reason": "the supplied path is not a readable Git checkout"}
    baseline = git.probe_baseline_ref(work)
    default_ref = None
    if baseline.get("ok") and baseline.get("items"):
        default_ref = baseline["items"][0]
    base["default_ref"] = default_ref
    ref = source_ref or default_ref or "HEAD"
    base["ref"] = ref
    resolved = _show_commit(work, ref)
    if resolved is None:
        return {**base, "verdict": "UNKNOWN",
                "reason": "the source reference could not be resolved"}
    head, epoch = resolved
    stale: bool | None = None
    if cutoff_epoch is not None:
        stale = epoch < cutoff_epoch
    return {
        **base,
        "verdict": "SUPPORTED",
        "reason": None,
        "head": head,
        "commit_epoch": epoch,
        "commit_at": _iso_from_epoch(epoch),
        "stale": stale,
    }


# ---------------------------------------------------------------------------
# Read-only team and Registry evidence (existing CAO state, when available)
# ---------------------------------------------------------------------------


def _registry_db_path(env: Mapping[str, str] | None) -> str:
    """The governed Work Registry ledger, resolved by the product config.

    Delegated to ``product_config.registry_db_path`` so B3 reads exactly the
    ledger the product task layer attaches to — ``CAO_REGISTRY_DB``, then the
    product config's ``registry_db``, then the isolated XDG default — instead of
    guessing a path of its own.
    """

    return product_config.registry_db_path(env)


def _registry_evidence(
    env: Mapping[str, str] | None, body: str
) -> dict[str, Any]:
    """A read-only snapshot of active Registry claims, or explicit UNKNOWN.

    The Registry is opened through ``connect_readonly`` (SQLite ``mode=ro``), so
    a write is impossible by construction and an absent database is reported
    rather than created. Only identifiers and a lexical overlap score leave this
    function; no Registry objective text is ever carried into the packet.
    """

    base: dict[str, Any] = {
        "source": "registry",
        "active_claims": [],
        "overlaps": [],
    }
    try:
        path = _registry_db_path(env)
    except Exception:  # noqa: BLE001 - an unresolvable location is UNKNOWN
        return {**base, "verdict": "UNKNOWN",
                "reason": "the local Work Registry location could not be resolved"}
    if not os.path.isfile(path):
        return {**base, "verdict": "UNAVAILABLE",
                "reason": "no local Work Registry was found"}
    try:
        conn = registry.connect_readonly(path)
    except Exception:  # noqa: BLE001 - a local read failure is UNKNOWN
        return {**base, "verdict": "UNKNOWN",
                "reason": "the local Work Registry could not be read"}
    try:
        claims = registry.list_active_claims(conn)
    except Exception:  # noqa: BLE001 - a local read failure is UNKNOWN
        return {**base, "verdict": "UNKNOWN",
                "reason": "the local Work Registry could not be read"}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - closing is best-effort
            pass
    tokens = registry.objective_tokens({"objective": body})
    active: list[dict[str, Any]] = []
    overlaps: list[dict[str, Any]] = []
    for claim in claims[:64]:
        run_id = claim.get("run_id")
        work_key = claim.get("work_key")
        if not isinstance(run_id, str) or not isinstance(work_key, str):
            continue
        active.append(
            {"run_id": run_id[:MAX_IDENT], "work_key": work_key[:MAX_IDENT],
             "mode": str(claim.get("mode") or "")[:16]}
        )
        score = registry.lexical_similarity(
            tokens, registry.objective_tokens(claim)
        )
        if score >= registry.OVERLAP_THRESHOLD:
            overlaps.append(
                {"run_id": run_id[:MAX_IDENT],
                 "work_key": work_key[:MAX_IDENT],
                 "score": round(float(score), 4)}
            )
    return {
        **base,
        "verdict": "SUPPORTED",
        "reason": None,
        "active_claims": active,
        "overlaps": sorted(
            overlaps, key=lambda item: (-item["score"], item["run_id"],
                                        item["work_key"])
        ),
    }


def _team_evidence(env: Mapping[str, str] | None) -> dict[str, Any]:
    """A read-only snapshot of existing CAO teams, or explicit UNAVAILABLE.

    ``team.list_teams`` is a pure read: an absent team directory is an honest
    empty list and a malformed team is contained by that module, never created
    here.
    """

    base: dict[str, Any] = {"source": "team", "teams": []}
    try:
        summaries = team.list_teams(env=env)
    except Exception:  # noqa: BLE001 - a local read failure is UNKNOWN
        return {**base, "verdict": "UNKNOWN",
                "reason": "local team state could not be read"}
    if not summaries:
        return {**base, "verdict": "UNAVAILABLE",
                "reason": "no local team state was found"}
    teams: list[dict[str, Any]] = []
    for item in summaries[:64]:
        if not isinstance(item, Mapping):
            continue
        teams.append(
            {
                "team_id": str(item.get("team_id") or "")[:MAX_IDENT],
                "desired": str(item.get("desired") or "")[:32],
                "status": str(item.get("status") or "")[:32],
            }
        )
    return {**base, "verdict": "SUPPORTED", "reason": None, "teams": teams}


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


def _risk_flags(body: str) -> list[str]:
    flags: list[str] = []
    for name, pattern in _RISK_PATTERNS:
        if pattern.search(body):
            flags.append(name)
    return flags


def _work_shaped(body: str) -> bool:
    return bool(_WORK_SHAPED.search(body))


def _question_shaped(body: str) -> bool:
    stripped = body.strip()
    if not stripped:
        return False
    if stripped.endswith("?"):
        return True
    return bool(_QUESTION_START.match(stripped))


def _request_digest(record: Mapping[str, Any]) -> str:
    """A one-way digest of the request content, independent of the item id.

    Two admissions of the same request under different item ids are genuine
    duplicates; the durable ``fingerprint`` cannot see that because it also binds
    the item id, so this digest is what duplicate detection compares.
    """

    return fingerprint(
        {
            "title": record["title"],
            "source": record["source"],
            "body": record["body"],
            "evidence_refs": record["evidence_refs"],
        }
    )


def _duplicate_signal(
    record: Mapping[str, Any], env: Mapping[str, str] | None
) -> dict[str, Any]:
    """Detect an identical-content submission, citing the other record.

    Reads the operator's own private submissions through intake's strict reader
    and compares a request-content digest that is independent of the item id. The
    scan is bounded, and an unreadable or corrupt sibling raises rather than
    being skipped: "could not check" is never reported as "no duplicate".
    """

    current = record["submission_id"]
    digest = _request_digest(record)
    scanned = 0
    truncated = False
    for view in intake.list_submissions(env):
        if view["submission_id"] == current:
            continue
        if scanned >= _MAX_DUPLICATE_SCAN:
            truncated = True
            break
        scanned += 1
        other = intake.read_submission(view["submission_id"], env)
        if _request_digest(other) == digest:
            return {
                "verdict": "SUPPORTED",
                "of": view["submission_id"],
                "item_id": view["item_id"],
                "request_fingerprint": digest,
                "basis": "an admitted submission carries the identical request "
                         "content (a digest independent of the item id)",
                "scanned": scanned,
                "truncated": False,
            }
    returned = {
        "verdict": "UNKNOWN",
        "of": None,
        "item_id": None,
        "request_fingerprint": None,
        "basis": "no other admitted submission shares this request content",
        "scanned": scanned,
        "truncated": truncated,
    }
    if truncated:
        returned["basis"] = (
            "the duplicate scan was bounded and did not cover every admitted "
            "submission, so no duplicate is asserted"
        )
    return returned


def _implemented_signal(
    implemented: str | None, git_evidence: Mapping[str, Any]
) -> dict[str, Any]:
    """Report an already-implemented request only with attributable evidence."""

    if implemented is None:
        return {
            "verdict": "UNKNOWN",
            "commit": None,
            "commit_at": None,
            "basis": "no already-implemented commit was supplied for verification",
        }
    repo = git_evidence.get("repo")
    resolved = _show_commit(repo, implemented) if isinstance(repo, str) else None
    if resolved is None:
        return {
            "verdict": "UNKNOWN",
            "commit": None,
            "commit_at": None,
            "basis": "the supplied already-implemented commit could not be "
                     "verified in the supplied repository",
        }
    commit, epoch = resolved
    return {
        "verdict": "SUPPORTED",
        "commit": commit,
        "commit_at": _iso_from_epoch(epoch),
        "basis": "the supplied commit resolving in the supplied repository is "
                 "the attributable evidence that the request is already "
                 "implemented",
    }


def _priority_signal(body: str) -> dict[str, Any]:
    match = _PRIORITY_MARKER.search(body)
    if match:
        return {
            "verdict": "SUPPORTED",
            "level": "HIGH",
            "basis": "an explicit priority marker is present in the request text",
            "marker": match.group(0).lower()[:32],
        }
    return {
        "verdict": "UNKNOWN",
        "level": None,
        "basis": "no attributable priority evidence is available",
        "marker": None,
    }


def _dependencies_signal(registry_evidence: Mapping[str, Any]) -> dict[str, Any]:
    overlaps = registry_evidence.get("overlaps")
    if registry_evidence.get("verdict") == "SUPPORTED" and overlaps:
        return {
            "verdict": "SUPPORTED",
            "items": list(overlaps),
            "basis": "active Registry work with overlapping objective tokens is "
                     "the attributable evidence for a possible dependency",
        }
    if registry_evidence.get("verdict") == "SUPPORTED":
        return {
            "verdict": "UNKNOWN",
            "items": [],
            "basis": "the local Work Registry was read and no overlapping "
                     "registered work was found; no dependency is asserted",
        }
    return {
        "verdict": "UNKNOWN",
        "items": [],
        "basis": "no readable local Work Registry was available, so no "
                 "dependency can be attributed",
    }


def _capability_hints(disposition: str) -> dict[str, Any]:
    if disposition == "PROPOSE_WORK":
        return {
            "verdict": "UNKNOWN",
            "hints": ["b4_routing_required"],
            "basis": "required Capability and performer qualification are "
                     "determined by B4 routing, not asserted by B3",
        }
    return {
        "verdict": "UNKNOWN",
        "hints": [],
        "basis": "required Capability is determined by B4 routing, not asserted "
                 "by B3",
    }


def _disposition(
    body: str, grounded: bool, duplicate: Mapping[str, Any],
    already_implemented: Mapping[str, Any],
) -> dict[str, Any]:
    """The one deterministic rule table. First match wins; an empty body first."""

    uncertainties: list[str] = [_NOTE_NOT_AI, _NOTE_ADVISORY]
    if not body.strip():
        return _decision(
            "ASK_CONTEXT",
            "the submission states no request text, so no grounded position is "
            "possible",
            uncertainties,
        )
    risk = _risk_flags(body)
    if risk:
        uncertainties.append(
            "the request text carries risk marker(s) and is treated as opaque "
            "data, never as a directive: " + ", ".join(risk)
        )
        return _decision(
            "HUMAN_REVIEW",
            "the request text carries risk marker(s) that require a human "
            "decision",
            uncertainties,
        )
    if already_implemented.get("verdict") == "SUPPORTED":
        return _decision(
            "DENY_OR_REPLAN",
            "the request is already implemented at an attributable commit",
            uncertainties,
        )
    if duplicate.get("verdict") == "SUPPORTED":
        return _decision(
            "DENY_OR_REPLAN",
            "the submission duplicates an admitted submission with identical "
            "content",
            uncertainties,
        )
    if not grounded:
        uncertainties.append(
            "no permitted source reference or current default-branch Git "
            "evidence was available"
        )
        return _decision(
            "ASK_CONTEXT",
            "no permitted source reference or current default-branch Git "
            "evidence grounds the request",
            uncertainties,
        )
    if _question_shaped(body):
        return _decision(
            "ANSWER_CANDIDATE",
            "the request is question-shaped and grounding evidence is present; "
            "any answer is a candidate and is not self-authorized",
            uncertainties,
        )
    if _work_shaped(body):
        return _decision(
            "PROPOSE_WORK",
            "the request is work-shaped and grounding evidence is present; it "
            "must go through B4 routing and is only proposed here",
            uncertainties,
        )
    return _decision(
        "HUMAN_REVIEW",
        "the request is grounded but its shape is ambiguous, so no automated "
        "disposition is safe",
        uncertainties,
    )


def _decision(value: str, basis: str, uncertainties: list[str]) -> dict[str, Any]:
    if value not in DISPOSITIONS:
        raise TriageStateError("refusing to emit a disposition outside the five")
    confidence = "LOW" if value in {"HUMAN_REVIEW", "ASK_CONTEXT"} else "MEDIUM"
    return {
        "value": value,
        "basis": basis[:MAX_TEXT],
        "confidence": confidence,
        "uncertainties": uncertainties[:_MAX_UNCERTAINTIES],
    }


# ---------------------------------------------------------------------------
# Packet construction
# ---------------------------------------------------------------------------


def triage_dir(env: Mapping[str, str] | None = None) -> str:
    """The private advisory-packet directory under the XDG CAO state root."""

    return os.path.join(state_dir(env), "intake-triage")


def packet_path(packet_id: str, env: Mapping[str, str] | None = None) -> str:
    return os.path.join(triage_dir(env), _record_id(packet_id) + ".json")


def packet_id_for(inputs: Mapping[str, Any]) -> str:
    """The deterministic packet id: a one-way digest of the evidence bundle."""

    return hashlib.sha256(canonical_json(inputs).encode("utf-8")).hexdigest()


def _options(
    repo: str | None, source_ref: str | None, cutoff: str | None,
    implemented: str | None,
) -> dict[str, Any]:
    if repo is not None:
        _bounded_str(repo, "repo", MAX_TEXT, allow_empty=False)
        if "\x00" in repo:
            raise TriageInvalid("repo must not contain a NUL byte")
    if source_ref is not None:
        if not (git.valid_commit_id(source_ref) or git.valid_short_ref(source_ref)):
            raise TriageInvalid("source_ref must be a commit id or a valid ref name")
    cutoff_iso = cutoff_epoch = None
    if cutoff is not None:
        cutoff_iso, cutoff_epoch = _parse_cutoff(cutoff)
    if implemented is not None and not git.valid_commit_id(implemented):
        raise TriageInvalid("implemented must be a 40-hex commit id")
    return {
        "repo": repo,
        "source_ref": source_ref,
        "cutoff": cutoff_iso,
        "cutoff_epoch": cutoff_epoch,
        "implemented": implemented,
    }


def _submission_block(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "submission_id": record["submission_id"],
        "item_id": record["item_id"],
        "fingerprint": record["fingerprint"],
        "source": record["source"],
        "title": record["title"],
        "body_redacted": record["body_redacted"],
        "evidence_refs": [dict(ref) for ref in record["evidence_refs"]],
    }


def _provenance_sources(
    submission: Mapping[str, Any], git_evidence: Mapping[str, Any],
    registry_evidence: Mapping[str, Any], team_evidence: Mapping[str, Any],
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = [
        {
            "kind": "submission",
            "ref": submission["submission_id"],
            "fingerprint": submission["fingerprint"],
            "verdict": "SUPPORTED",
            "reason": None,
        }
    ]
    for ref in submission["evidence_refs"][:_MAX_REFS]:
        sources.append(
            {
                "kind": "evidence_ref",
                "ref": ref["ref"],
                "fingerprint": ref.get("fingerprint"),
                "verdict": "SUPPORTED",
                "reason": None,
            }
        )
    for evidence in (git_evidence, registry_evidence, team_evidence):
        sources.append(
            {
                "kind": evidence["source"],
                "ref": str(evidence.get("repo") or evidence.get("ref") or
                           evidence["source"]),
                "fingerprint": fingerprint(_without_reason(evidence)),
                "verdict": evidence["verdict"],
                "reason": evidence.get("reason"),
            }
        )
    return sources[:MAX_SOURCES]


def _without_reason(evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in evidence.items()
            if key not in {"reason", "verdict"}}


def build_packet(
    record: Mapping[str, Any],
    *,
    options: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Assemble one advisory packet deterministically from read-only evidence.

    Pure with respect to authority: it reads the submission, Git, the local
    Registry and local team state and returns a packet. It writes nothing, claims
    nothing, starts nothing and sends nothing.
    """

    if not isinstance(record, Mapping):
        raise TriageInvalid("submission record must be a mapping")
    body = record.get("body")
    if not isinstance(body, str):
        raise TriageInvalid("submission record has no readable body")

    git_evidence = _git_evidence(
        options.get("repo"), options.get("source_ref"),
        options.get("cutoff"), options.get("cutoff_epoch"),
    )
    registry_evidence = _registry_evidence(env, body)
    team_evidence = _team_evidence(env)
    duplicate = _duplicate_signal(record, env)
    already_implemented = _implemented_signal(
        options.get("implemented"), git_evidence
    )

    refs = record.get("evidence_refs") or []
    grounded = bool(refs) or (
        git_evidence["verdict"] == "SUPPORTED" and git_evidence["stale"] is not True
    )
    disposition = _disposition(body, grounded, duplicate, already_implemented)

    submission = _submission_block(record)
    inputs = {
        "submission_fingerprint": record["fingerprint"],
        "options": {
            "repo": options.get("repo"),
            "source_ref": options.get("source_ref"),
            "cutoff": options.get("cutoff"),
            "implemented": options.get("implemented"),
        },
        "git": git_evidence,
        "source_refs": [dict(ref) for ref in refs[:_MAX_REFS]],
        "registry": registry_evidence,
        "team": team_evidence,
    }
    packet_id = packet_id_for(inputs)
    stale_reason = None
    if git_evidence.get("stale") is True:
        stale_reason = (
            "the resolved source commit predates the supplied evidence cutoff; "
            "it is reported stale and is not used as current grounding"
        )
    packet: dict[str, Any] = {
        "schema": ADVISORY_SCHEMA,
        "record_type": RECORD_TYPE,
        "packet_id": packet_id,
        "advisory": True,
        "authority": "none",
        "created_at": utc_now(clock),
        "content_fingerprint": "",
        "submission": submission,
        "inputs": inputs,
        "disposition": disposition,
        "signals": {
            "grounded": grounded,
            "question_shaped": _question_shaped(body),
            "work_shaped": _work_shaped(body),
            "risk_flags": _risk_flags(body),
            "duplicate": duplicate,
            "already_implemented": already_implemented,
            "priority": _priority_signal(body),
            "dependencies": _dependencies_signal(registry_evidence),
            "capability_hints": _capability_hints(disposition["value"]),
        },
        "provenance": {
            # The evidence cutoff is deterministic: the operator's requested
            # cutoff plus the resolved source ref/head/time. The moment of the
            # triage itself is ``created_at``, which is deliberately outside the
            # content fingerprint so an identical replay collapses to one packet.
            "evidence_cutoff": {
                "requested": options.get("cutoff"),
                "repo": git_evidence.get("repo"),
                "source_ref": git_evidence.get("ref"),
                "source_head": git_evidence.get("head"),
                "source_commit_at": git_evidence.get("commit_at"),
            },
            "stale": git_evidence.get("stale") is True,
            "stale_reason": stale_reason,
            "sources": _provenance_sources(
                submission, git_evidence, registry_evidence, team_evidence
            ),
            "uncertainties": list(dict.fromkeys(disposition["uncertainties"])),
        },
        "ai": {
            "used": False,
            "provider": None,
            "egress": "none",
            "reason": _NO_EGRESS_REASON,
        },
    }
    packet["content_fingerprint"] = _content_fingerprint(packet)
    return packet


def _content_fingerprint(packet: Mapping[str, Any]) -> str:
    content = {
        key: value
        for key, value in packet.items()
        if key not in {"packet_id", "created_at", "content_fingerprint"}
    }
    return fingerprint(content)


# ---------------------------------------------------------------------------
# Strict, bounded packet verification
# ---------------------------------------------------------------------------


def _verify_packet(data: Any, expected_id: str) -> dict[str, Any]:
    """Verify one stored packet against its file name and its own content."""

    if not isinstance(data, Mapping):
        raise TriageStateError("stored advisory packet is not a JSON object")
    if data.get("schema") != ADVISORY_SCHEMA:
        raise TriageStateError("stored advisory packet has an unknown schema")
    if set(data) != _PACKET_KEYS:
        raise TriageStateError("stored advisory packet has an unexpected shape")
    if data.get("record_type") != RECORD_TYPE:
        raise TriageStateError("stored record is not an advisory packet")
    if data.get("advisory") is not True or data.get("authority") != "none":
        raise TriageStateError("stored advisory packet is not advisory-only")
    try:
        declared = _record_id(data.get("packet_id"))
        _bounded_str(data.get("created_at"), "created_at", 64, allow_empty=False)
        stored_print = _bounded_str(
            data.get("content_fingerprint"), "content_fingerprint", 128,
            allow_empty=False,
        )
    except TriageInvalid as exc:
        raise TriageStateError("stored advisory packet is malformed") from exc
    if declared != expected_id:
        raise TriageStateError("stored packet id does not match its file")
    submission = data.get("submission")
    if not isinstance(submission, Mapping) or set(submission) != _SUBMISSION_KEYS:
        raise TriageStateError("stored advisory submission block is malformed")
    if not isinstance(submission.get("body_redacted"), bool):
        raise TriageStateError("stored advisory body_redacted is not a boolean")
    inputs = data.get("inputs")
    if not isinstance(inputs, Mapping) or set(inputs) != _INPUT_KEYS:
        raise TriageStateError("stored advisory inputs block is malformed")
    disposition = data.get("disposition")
    if not isinstance(disposition, Mapping) or set(disposition) != _DISPOSITION_KEYS:
        raise TriageStateError("stored advisory disposition block is malformed")
    if disposition.get("value") not in DISPOSITIONS:
        raise TriageStateError("stored advisory disposition is not one of the five")
    signals = data.get("signals")
    if not isinstance(signals, Mapping) or set(signals) != _SIGNAL_KEYS:
        raise TriageStateError("stored advisory signals block is malformed")
    provenance = data.get("provenance")
    if not isinstance(provenance, Mapping) or set(provenance) != _PROVENANCE_KEYS:
        raise TriageStateError("stored advisory provenance block is malformed")
    sources = provenance.get("sources")
    if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
        raise TriageStateError("stored advisory provenance sources are malformed")
    for source in sources:
        if not isinstance(source, Mapping) or set(source) != _SOURCE_KEYS:
            raise TriageStateError("stored advisory provenance source is malformed")
        if source.get("verdict") not in _ERROR_VERDICTS:
            raise TriageStateError("stored advisory source verdict is unknown")
    ai = data.get("ai")
    if not isinstance(ai, Mapping) or set(ai) != _AI_KEYS:
        raise TriageStateError("stored advisory ai block is malformed")
    if ai.get("used") is not False or ai.get("egress") != "none":
        raise TriageStateError("stored advisory packet claims provider use or egress")
    try:
        if packet_id_for(inputs) != declared:
            raise TriageStateError("stored packet id does not bind its evidence")
    except (TypeError, ValueError) as exc:
        raise TriageStateError("stored advisory inputs are not canonical") from exc
    if _content_fingerprint(data) != stored_print:
        raise TriageStateError(
            "stored advisory content does not match its fingerprint"
        )
    return dict(data)


# ---------------------------------------------------------------------------
# Persist one advisory packet (private, atomic, idempotent)
# ---------------------------------------------------------------------------


def _admission(status: str, packet: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": ADVISORY_SCHEMA,
        "status": status,
        "duplicate": status == "DUPLICATE",
        "packet": dict(packet),
    }


def triage(
    submission_id: str,
    *,
    env: Mapping[str, str] | None = None,
    repo: str | None = None,
    source_ref: str | None = None,
    cutoff: str | None = None,
    implemented: str | None = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Triage one admitted submission on demand and persist the advisory packet.

    A repeated triage of the same submission against the same evidence yields
    ``DUPLICATE`` with the original packet; different evidence yields a distinct
    packet, never a silent merge. Nothing is started, claimed or sent.
    """

    sid = _submission_id(submission_id)
    record = intake.read_submission(sid, env)
    options = _options(repo, source_ref, cutoff, implemented)
    packet = build_packet(record, options=options, env=env, clock=clock)
    name = packet["packet_id"] + ".json"
    payload = json.dumps(
        packet, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False
    )
    if len(payload.encode("utf-8")) > MAX_PACKET_BYTES:
        raise TriageInvalid(f"advisory packet exceeds {MAX_PACKET_BYTES} bytes")
    dir_fd = intake.open_private_dir(triage_dir(env))
    try:
        created = intake.write_private_record(dir_fd, name, payload)
        data = intake.read_private_record(dir_fd, name)
    finally:
        os.close(dir_fd)
    if created:
        if data is None:
            raise TriageStateError("advisory packet failed its read-back check")
        stored = _verify_packet(data, packet["packet_id"])
        if stored != packet:
            raise TriageStateError("advisory packet failed its read-back check")
        return _admission("ADMITTED", packet)
    if data is None:
        raise TriageUnavailable("an existing advisory packet disappeared before read")
    stored = _verify_packet(data, packet["packet_id"])
    if stored["content_fingerprint"] == packet["content_fingerprint"]:
        return _admission("DUPLICATE", stored)
    raise TriageConflict(
        "advisory packet id already exists with different content; refusing to "
        "merge"
    )


def public_view(packet: Mapping[str, Any]) -> dict[str, Any]:
    """A bounded summary of one packet for listing (no body, no raw evidence)."""

    return {
        "packet_id": packet["packet_id"],
        "submission_id": packet["submission"]["submission_id"],
        "item_id": packet["submission"]["item_id"],
        "disposition": packet["disposition"]["value"],
        "created_at": packet["created_at"],
    }


def list_packets(
    env: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Bounded summaries of every stored packet, oldest first, fail-closed.

    An absent triage directory is an empty list. An existing but invalid path or
    a corrupt packet raises rather than being skipped.
    """

    directory = triage_dir(env)
    dir_fd = intake.open_private_dir_readonly(directory)
    if dir_fd is None:
        return []
    try:
        views: list[dict[str, Any]] = []
        for name in intake.list_private_names(dir_fd):
            if name.startswith(".") or not name.endswith(".json"):
                continue
            packet_id = name[: -len(".json")]
            if not _ID_RE.fullmatch(packet_id):
                raise TriageStateError("triage directory holds an unexpected file")
            data = intake.read_private_record(dir_fd, name)
            if data is None:
                continue
            views.append(public_view(_verify_packet(data, packet_id)))
    finally:
        os.close(dir_fd)
    views.sort(key=lambda view: (view["created_at"], view["packet_id"]))
    return views


def read_packet(
    packet_id: str, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Read one full advisory packet, or fail closed."""

    pid = _record_id(packet_id)
    dir_fd = intake.open_private_dir_readonly(triage_dir(env))
    if dir_fd is None:
        raise TriageUnavailable("no triage advisory packet store exists")
    try:
        data = intake.read_private_record(dir_fd, pid + ".json")
    finally:
        os.close(dir_fd)
    if data is None:
        raise TriageUnavailable("no advisory packet with that id")
    return _verify_packet(data, pid)
