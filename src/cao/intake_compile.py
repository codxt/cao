"""Governed routing and manifest compiler for triaged proposals (B4).

B4 is the *compile* half of the governed intake middle. Given exactly one
already-persisted B3 advisory packet whose disposition is ``PROPOSE_WORK`` and a
strictly bounded, operator-supplied **route evidence** document, it matches the
proposal to an accountable owning source and compiles one **immutable,
reviewable draft** ``cao.team.v1`` manifest intended for **existing** CAO Team
Execution.

It is deliberately small in effect and holds **no execution authority**:

* a compiled draft is a *proposal for review*. It is not approval, not a work
  item, not a Work Registry claim and not canonical Company State;
* it starts, merges, dispatches, schedules, claims, sends and deploys nothing,
  and it writes no Registry row. It reads only;
* the route evidence is bounded operator input, never automatic approval. A
  route file cannot authorize anything: it can only supply the accepted
  Plan/objective/work package, the required Capability, the performer and the
  authority *evidence* that an accountable owner must still review;
* every load-bearing claim is anchored to an immutable Git identity and verified
  with read-only Git evidence: the exact repository, resolved ref, commit id and
  committer time, and the content digest of the cited document at that commit.
  A claim that cannot be objectively established yields an explicit
  ``NEEDS_HUMAN_REVIEW`` or ``UNAVAILABLE`` result instead of a qualified
  manifest. Mere free text such as ``"accepted": true`` is never authority, and
  no model assertion is trusted;
* a compiled draft is always ``proposed`` with ``authority = "none"`` and
  ``dispatch = false``. The actual owner must separately review it and run
  ``cao team init --approved`` before any governed dispatch is considered.

Human, software and hybrid performers remain **advisory** here: CAO's existing
Team Execution adapter launches AI specialists only, so a non-AI performer
yields an explicit ``UNAVAILABLE`` rather than a falsely launchable manifest.

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

from . import (git, intake, intake_triage, product_config, registry, team)
from .evidence import canonical_json, fingerprint, utc_now
from .product_state import state_dir

__all__ = [
    "ROUTE_SCHEMA",
    "DRAFT_SCHEMA",
    "RECORD_TYPE",
    "RESULT_SCHEMA",
    "PERFORMER_KINDS",
    "SUPPORTED_PERFORMER_KINDS",
    "MAX_TEXT",
    "MAX_IDENT",
    "MAX_PATHS",
    "MAX_PLAN_DOC_BYTES",
    "CompileError",
    "CompileInvalid",
    "CompileUnavailable",
    "CompileStateError",
    "CompileConflict",
    "CompileNeedsReview",
    "route_dir",
    "draft_path",
    "draft_id_for",
    "verify_source",
    "compile_route",
    "list_drafts",
    "read_draft",
    "public_view",
]

#: The strict schema an operator-supplied route evidence document must carry.
ROUTE_SCHEMA = "cao.intake.route.v1"
#: The schema of one persisted, reviewable draft manifest envelope.
DRAFT_SCHEMA = "cao.intake.route.draft.v1"
#: The record type that separates a B4 draft from a submission, a triage packet
#: and any canonical accepted work or live team.
RECORD_TYPE = "intake_route_draft"
#: The JSON schema B4 returns from its CLI surface.
RESULT_SCHEMA = "cao.intake.route.result.v1"

#: Every performer kind the doctrine names. Only kinds with an existing Team
#: Execution adapter may compile into a launchable manifest.
PERFORMER_KINDS = ("ai", "human", "software", "hybrid")
#: The existing Team Execution path launches AI specialists only.
SUPPORTED_PERFORMER_KINDS = ("ai",)

MAX_TEXT = 2000
MAX_IDENT = 64
MAX_PATHS = 64
#: The largest cited source document this module will read from Git. The plan,
#: qualification and grant records are bounded evidence, not payloads.
MAX_PLAN_DOC_BYTES = 256 * 1024

_ID_RE = re.compile(r"[0-9a-f]{64}")
_IDENT_RE = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
_STAMP_RE = re.compile(r"[0-9]{1,12}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")

_ROUTE_KEYS = frozenset(
    {"schema", "packet", "plan", "capability", "authority", "scope", "manifest"}
)
_PACKET_KEYS = frozenset({"packet_id", "content_fingerprint"})
_PLAN_KEYS = frozenset(
    {
        "plan_id",
        "initiative",
        "objective",
        "work_package",
        "owner",
        "reviewer",
        "accepted_at",
        "approval_digest",
        "source",
    }
)
_CAPABILITY_KEYS = frozenset({"required", "performer_kind", "source"})
_AUTHORITY_KEYS = frozenset({"grantor", "grant_digest", "source"})
_SCOPE_KEYS = frozenset({"repo", "domain", "paths"})
_MANIFEST_KEYS = frozenset(
    {"team_id", "roles", "tasks", "max_writers", "max_parallel"}
)
_SOURCE_KEYS = frozenset({"repo", "ref", "head", "path", "commit_at", "digest"})

_DRAFT_KEYS = frozenset(
    {
        "schema",
        "record_type",
        "draft_id",
        "status",
        "authority",
        "dispatch",
        "requires_review",
        "created_at",
        "content_fingerprint",
        "packet",
        "plan",
        "capability",
        "authority_grant",
        "scope",
        "manifest",
        "provenance",
        "review",
        "uncertainties",
    }
)

_NOTE_NOT_AUTHORITY = (
    "a compiled draft is a proposal for review only; it carries no authority"
)
_NOTE_OWNER_STEP = (
    "the accountable owner must independently review this draft and run "
    "'cao team init --approved' before any governed dispatch is considered"
)
_NOTE_PROVIDER = (
    "role model selection is configuration, not authority; no provider is "
    "certified suitable for private/tenant/secret data by this draft"
)


class CompileError(Exception):
    """Base class for B4 routing/compilation failures."""

    code = "ROUTE_ERROR"


class CompileInvalid(CompileError):
    """The route evidence, the packet or an option is malformed or unusable."""

    code = "ROUTE_INVALID"


class CompileUnavailable(CompileError):
    """Routing is refused: an effect-free precondition cannot be met."""

    code = "ROUTE_UNAVAILABLE"


class CompileStateError(CompileError):
    """Persisted B4 state is present but not usable."""

    code = "ROUTE_STATE"


class CompileConflict(CompileError):
    """A draft id was reused for different content."""

    code = "ROUTE_CONFLICT"


class CompileNeedsReview(CompileError):
    """A load-bearing claim is not objectively established; a human decides."""

    code = "NEEDS_HUMAN_REVIEW"

    def __init__(self, message: str, reasons: list[str] | None = None) -> None:
        super().__init__(message)
        self.reasons = list(reasons or [])


# ---------------------------------------------------------------------------
# Bounded validation helpers
# ---------------------------------------------------------------------------


def _bounded_str(value: Any, field: str, limit: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise CompileInvalid(f"{field} must be a string")
    if not allow_empty and not value:
        raise CompileInvalid(f"{field} must be a non-empty string")
    if len(value) > limit:
        raise CompileInvalid(f"{field} exceeds {limit} characters")
    if "\x00" in value:
        raise CompileInvalid(f"{field} must not contain a NUL byte")
    return value


def _ident(value: Any, field: str) -> str:
    text = _bounded_str(value, field, MAX_IDENT, allow_empty=False)
    if not _IDENT_RE.fullmatch(text):
        raise CompileInvalid(f"{field} must be a bounded lowercase identifier")
    return text


def _record_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise CompileInvalid(f"{field} must be 64 lowercase hex characters")
    return value


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise CompileInvalid(f"{field} must be a sha256: digest")
    return value


def _fields(value: Any, allowed: frozenset[str], label: str) -> dict:
    if type(value) is not dict or set(value) != allowed:
        raise CompileInvalid(f"{label} has missing or unsupported field(s)")
    return value


def _optional_fields(
    value: Any, allowed: frozenset[str], required: frozenset[str], label: str
) -> dict:
    if (type(value) is not dict or set(value) - allowed
            or required - set(value)):
        raise CompileInvalid(f"{label} has missing or unsupported field(s)")
    return value


def _iso_from_epoch(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_text(value: Any, field: str, limit: int) -> str:
    """Bounded review text that must never carry an obvious credential.

    Route evidence becomes part of a reviewable manifest, so a credential-shaped
    value is refused rather than silently persisted or redacted into different
    content.
    """

    text = _bounded_str(value, field, limit, allow_empty=False)
    if intake.looks_like_credential(text):
        raise CompileInvalid(
            f"{field} is credential-shaped and is refused rather than persisted"
        )
    return text


# ---------------------------------------------------------------------------
# Bounded read-only Git evidence (one exact argv shape, no shell)
# ---------------------------------------------------------------------------

#: The two Git command shapes this module may run: a commit id/time observation
#: and a bounded blob read. An argv that is not exactly one of these cannot
#: reach git.
_HEAD_ARGV = ("show", "-s", "--format=%H %ct")


def _valid_blob_operand(value: Any) -> bool:
    if type(value) is not str or value.count(":") != 1:
        return False
    commit, path = value.split(":", 1)
    return git.valid_commit_id(commit) and git.valid_relpath(path)


def _allowed_git_argv(args: Any) -> bool:
    if type(args) is not list:
        return False
    if len(args) == 4 and tuple(args[:3]) == _HEAD_ARGV:
        operand = args[3]
        return type(operand) is str and (
            git.valid_commit_id(operand) or git.valid_short_ref(operand)
        )
    if len(args) == 3 and args[0] == "cat-file" and args[1] in ("-s", "blob"):
        return _valid_blob_operand(args[2])
    return False


def _child_env() -> dict[str, str]:
    # A minimal child environment. Only PATH, HOME and TMPDIR are forwarded, so
    # a caller's GIT_DIR, GIT_WORK_TREE or any other GIT_* override cannot
    # redirect this observation to an unrelated repository.
    env = {"GIT_OPTIONAL_LOCKS": "0"}
    for name in ("PATH", "HOME", "TMPDIR"):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env


def _git_argv(args: list[str]) -> list[str]:
    argv = ["git", "--no-optional-locks", "-c", "diff.autoRefreshIndex=false"]
    argv.extend(args)
    return argv


def _run_git(repo: str, args: list[str]) -> tuple[int | None, str]:
    """Run one ALLOWLISTED read-only git observation. Never raises."""

    if not _allowed_git_argv(args):
        return None, ""
    try:
        proc = subprocess.run(
            _git_argv(args),
            cwd=repo,
            env=_child_env(),
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


def _read_blob(repo: str, commit: str, path: str) -> bytes | None:
    """Read one blob at an exact commit, bounded, or ``None``.

    The size is checked first, then the content is read through a bounded pipe
    that is closed as soon as the bound is exceeded, so an oversized object can
    never be buffered whole.
    """

    operand = f"{commit}:{path}"
    if not _valid_blob_operand(operand):
        return None
    rc, out = _run_git(repo, ["cat-file", "-s", operand])
    if rc != 0:
        return None
    size_text = out.strip()
    if not size_text.isdigit():
        return None
    size = int(size_text)
    if size < 0 or size > MAX_PLAN_DOC_BYTES:
        return None
    args = _git_argv(["cat-file", "blob", operand])
    if not _allowed_git_argv(["cat-file", "blob", operand]):
        return None
    try:
        proc = subprocess.Popen(
            args,
            cwd=repo,
            env=_child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001 - a probe never propagates
        return None
    try:
        data = proc.stdout.read(MAX_PLAN_DOC_BYTES + 1) if proc.stdout else b""
    except Exception:  # noqa: BLE001 - an unreadable blob is not evidence
        data = b""
    finally:
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception:  # noqa: BLE001 - closing is best-effort
            pass
        try:
            proc.kill()
        except Exception:  # noqa: BLE001 - killing a finished child is a no-op
            pass
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - reaping is best-effort
            pass
    if data is None or len(data) != size:
        return None
    return data


# ---------------------------------------------------------------------------
# Route document validation
# ---------------------------------------------------------------------------


def _validate_source(value: Any, label: str) -> dict[str, Any]:
    source = _fields(value, _SOURCE_KEYS, label)
    repo = _bounded_str(source["repo"], f"{label}.repo", 1024, allow_empty=False)
    ref = _bounded_str(source["ref"], f"{label}.ref", 256, allow_empty=False)
    if not git.valid_short_ref(ref):
        raise CompileInvalid(f"{label}.ref must be a valid Git short ref name")
    head = _bounded_str(source["head"], f"{label}.head", 64, allow_empty=False)
    if not git.valid_commit_id(head):
        raise CompileInvalid(f"{label}.head must be a 40-hex commit id")
    path = _bounded_str(source["path"], f"{label}.path", 1024, allow_empty=False)
    if not git.valid_relpath(path):
        raise CompileInvalid(f"{label}.path must be a canonical repo-relative path")
    commit_at = _bounded_str(
        source["commit_at"], f"{label}.commit_at", 64, allow_empty=False
    )
    digest = _digest(source["digest"], f"{label}.digest")
    return {
        "repo": repo,
        "ref": ref,
        "head": head,
        "path": path,
        "commit_at": commit_at,
        "digest": digest,
    }


def _validate_manifest(value: Any) -> dict[str, Any]:
    manifest = _optional_fields(
        value, _MANIFEST_KEYS, frozenset({"team_id", "roles", "tasks"}),
        "manifest",
    )
    team_id = _ident(manifest["team_id"], "manifest.team_id")
    roles = manifest["roles"]
    if type(roles) is not dict:
        raise CompileInvalid("manifest.roles must be an object")
    tasks = manifest["tasks"]
    if type(tasks) is not list:
        raise CompileInvalid("manifest.tasks must be an array")
    return {
        "team_id": team_id,
        "roles": roles,
        "tasks": tasks,
        **({"max_writers": manifest["max_writers"]}
           if "max_writers" in manifest else {}),
        **({"max_parallel": manifest["max_parallel"]}
           if "max_parallel" in manifest else {}),
    }


def _validate_route(document: Any, packet: Mapping[str, Any]) -> dict[str, Any]:
    route = _fields(document, _ROUTE_KEYS, "route")
    if route["schema"] != ROUTE_SCHEMA:
        raise CompileInvalid(f"route schema must be {ROUTE_SCHEMA!r}")

    block = _fields(route["packet"], _PACKET_KEYS, "route.packet")
    packet_id = _record_id(block["packet_id"], "route.packet.packet_id")
    packet_print = _bounded_str(
        block["content_fingerprint"], "route.packet.content_fingerprint", 128,
        allow_empty=False,
    )
    if packet_id != packet["packet_id"]:
        raise CompileInvalid("route file does not name the triaged packet")
    if packet_print != packet["content_fingerprint"]:
        raise CompileInvalid("route file does not bind the triaged packet content")

    plan = _fields(route["plan"], _PLAN_KEYS, "route.plan")
    owner = _ident(plan["owner"], "route.plan.owner")
    reviewer = _ident(plan["reviewer"], "route.plan.reviewer")
    if owner == reviewer:
        raise CompileInvalid("route.plan.owner and reviewer must be distinct")
    validated_plan = {
        "plan_id": _ident(plan["plan_id"], "route.plan.plan_id"),
        "initiative": _ident(plan["initiative"], "route.plan.initiative"),
        "objective": _safe_text(plan["objective"], "route.plan.objective", MAX_TEXT),
        "work_package": _safe_text(
            plan["work_package"], "route.plan.work_package", MAX_TEXT
        ),
        "owner": owner,
        "reviewer": reviewer,
        "accepted_at": _bounded_str(
            plan["accepted_at"], "route.plan.accepted_at", 64, allow_empty=False
        ),
        "approval_digest": _digest(
            plan["approval_digest"], "route.plan.approval_digest"
        ),
        "source": _validate_source(plan["source"], "route.plan.source"),
    }

    capability = _fields(route["capability"], _CAPABILITY_KEYS, "route.capability")
    performer_kind = _bounded_str(
        capability["performer_kind"], "route.capability.performer_kind", 32,
        allow_empty=False,
    )
    if performer_kind not in PERFORMER_KINDS:
        raise CompileInvalid(
            "route.capability.performer_kind must be one of "
            + ", ".join(PERFORMER_KINDS)
        )
    validated_capability = {
        "required": _safe_text(
            capability["required"], "route.capability.required", 200
        ),
        "performer_kind": performer_kind,
        "source": _validate_source(
            capability["source"], "route.capability.source"
        ),
    }

    authority = _fields(route["authority"], _AUTHORITY_KEYS, "route.authority")
    validated_authority = {
        "grantor": _ident(authority["grantor"], "route.authority.grantor"),
        "grant_digest": _digest(
            authority["grant_digest"], "route.authority.grant_digest"
        ),
        "source": _validate_source(
            authority["source"], "route.authority.source"
        ),
    }

    scope = _fields(route["scope"], _SCOPE_KEYS, "route.scope")
    repo = _bounded_str(scope["repo"], "route.scope.repo", 1024, allow_empty=False)
    if not git.valid_abs_path(repo):
        raise CompileInvalid("route.scope.repo must be a canonical absolute path")
    raw_paths = scope["paths"]
    if type(raw_paths) is not list or not 1 <= len(raw_paths) <= MAX_PATHS:
        raise CompileInvalid(f"route.scope.paths must hold 1..{MAX_PATHS} entries")
    paths: list[str] = []
    for index, entry in enumerate(raw_paths):
        text = _bounded_str(
            entry, f"route.scope.paths[{index}]", 1024, allow_empty=False
        )
        normalized = text[:-1] if text.endswith("/") else text
        if not normalized or not git.valid_relpath(normalized):
            raise CompileInvalid("route.scope.paths entries must be repo-relative")
        paths.append(normalized)
    if len(set(paths)) != len(paths):
        raise CompileInvalid("route.scope.paths entries must be unique")
    validated_scope = {
        "repo": repo,
        "domain": _ident(scope["domain"], "route.scope.domain"),
        "paths": paths,
    }

    return {
        "packet": {"packet_id": packet_id, "content_fingerprint": packet_print},
        "plan": validated_plan,
        "capability": validated_capability,
        "authority": validated_authority,
        "scope": validated_scope,
        "manifest": _validate_manifest(route["manifest"]),
    }


# ---------------------------------------------------------------------------
# Objective provenance verification (read-only Git)
# ---------------------------------------------------------------------------


def verify_source(source: Mapping[str, Any], label: str) -> dict[str, Any]:
    """Verify one cited source document against read-only Git evidence.

    Establishes, objectively: the repository is a readable checkout, the cited
    ref resolves to the cited commit id with the cited committer time, and the
    document at ``<commit>:<path>`` exists with the cited content digest. Any
    failure is reported ``UNVERIFIED`` with a reason; it is never rounded up.
    """

    base: dict[str, Any] = {
        "label": label,
        "repo": source["repo"],
        "ref": source["ref"],
        "head": None,
        "commit_at": None,
        "path": source["path"],
        "digest": None,
    }
    work = os.path.realpath(os.path.expanduser(source["repo"]))
    if not os.path.isdir(work):
        return {**base, "verdict": "UNVERIFIED",
                "reason": "the cited repository path is not a directory"}
    trees = git.probe_worktrees(work)
    if not trees.get("ok"):
        return {**base, "verdict": "UNVERIFIED",
                "reason": "the cited path is not a readable Git checkout"}
    resolved = _show_commit(work, source["ref"])
    if resolved is None:
        return {**base, "verdict": "UNVERIFIED",
                "reason": "the cited ref could not be resolved"}
    head, epoch = resolved
    if head != source["head"]:
        return {**base, "head": head, "verdict": "UNVERIFIED",
                "reason": "the cited ref does not resolve to the cited commit id"}
    commit_at = _iso_from_epoch(epoch)
    if commit_at != source["commit_at"]:
        return {**base, "head": head, "commit_at": commit_at,
                "verdict": "UNVERIFIED",
                "reason": "the cited commit time does not match the resolved one"}
    data = _read_blob(work, head, source["path"])
    if data is None:
        return {**base, "head": head, "commit_at": commit_at,
                "verdict": "UNVERIFIED",
                "reason": "the cited document is not readable at the cited commit"}
    digest = "sha256:" + _sha256_hex(data)
    if digest != source["digest"]:
        return {**base, "head": head, "commit_at": commit_at, "digest": digest,
                "verdict": "UNVERIFIED",
                "reason": "the cited document content does not match the digest"}
    return {**base, "head": head, "commit_at": commit_at, "digest": digest,
            "verdict": "SUPPORTED", "reason": None}


# ---------------------------------------------------------------------------
# Registry overlap (read-only, same governed ledger triage reads)
# ---------------------------------------------------------------------------


def _registry_overlaps(
    env: Mapping[str, str] | None, objective: str
) -> dict[str, Any]:
    """Read-only active-claim overlap evidence, or an explicit verdict.

    An absent ledger answers honestly with no active claims. A present but
    unreadable ledger fails closed as ``UNKNOWN`` so an overlap is never
    asserted or denied without evidence.
    """

    base: dict[str, Any] = {"source": "registry", "active_claims": [],
                            "overlaps": []}
    try:
        ledger = product_config.registry_db_path(env)
    except Exception:  # noqa: BLE001 - an unresolvable location is UNKNOWN
        return {**base, "verdict": "UNKNOWN",
                "reason": "the local Work Registry location could not be resolved"}
    if not os.path.isfile(ledger):
        return {**base, "verdict": "UNAVAILABLE",
                "reason": "no local Work Registry was found"}
    try:
        conn = registry.connect_readonly(ledger)
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
    tokens = registry.objective_tokens({"objective": objective})
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
                {"run_id": run_id[:MAX_IDENT], "work_key": work_key[:MAX_IDENT],
                 "score": round(float(score), 4)}
            )
    return {
        **base,
        "verdict": "SUPPORTED",
        "reason": None,
        "active_claims": active,
        "overlaps": sorted(
            overlaps,
            key=lambda item: (-item["score"], item["run_id"], item["work_key"]),
        ),
    }


# ---------------------------------------------------------------------------
# Manifest compilation (shape-validated, scoped, immutable)
# ---------------------------------------------------------------------------


def _within(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def _compile_manifest(route: Mapping[str, Any]) -> dict[str, Any]:
    """Build one ``cao.team.v1`` manifest, shape-validated and scope-matched.

    ``team.validate_spec`` performs pure shape validation only; it authorizes
    nothing. Scope and reviewer matching are enforced here before the manifest
    is accepted, and every task repository must be the declared scope checkout.
    """

    scope = route["scope"]
    scope_repo = os.path.realpath(scope["repo"])
    manifest = route["manifest"]
    spec = {
        "schema": team.SPEC_SCHEMA,
        "team_id": manifest["team_id"],
        "initiative": route["plan"]["initiative"],
        "roles": manifest["roles"],
        "tasks": manifest["tasks"],
    }
    if "max_writers" in manifest:
        spec["max_writers"] = manifest["max_writers"]
    if "max_parallel" in manifest:
        spec["max_parallel"] = manifest["max_parallel"]
    try:
        normalized = team.validate_spec(spec)
    except team.TeamError as exc:
        raise CompileInvalid(f"manifest is not a valid cao.team.v1: {exc}") from exc

    review_roles = {task.get("reviewer") for task in normalized["tasks"]
                    if task.get("reviewer")}
    if not review_roles:
        raise CompileNeedsReview(
            "the proposed manifest declares no independent reviewer",
            ["no WRITE task names a distinct reviewer role"],
        )
    if route["plan"]["reviewer"] not in normalized["roles"]:
        raise CompileNeedsReview(
            "the accountable reviewer is not a declared manifest role",
            ["route.plan.reviewer is absent from the manifest roles"],
        )

    for task in normalized["tasks"]:
        task_repo = os.path.realpath(os.path.expanduser(task["repo"]))
        if task_repo != scope_repo:
            raise CompileNeedsReview(
                "a task targets a repository outside the declared scope",
                ["task repository is not the declared scope checkout"],
            )
        for entry in task.get("write_scope", []):
            if not any(_within(entry, prefix) for prefix in scope["paths"]):
                raise CompileNeedsReview(
                    "a WRITE scope entry falls outside the declared path scope",
                    ["task write_scope escapes route.scope.paths"],
                )
    return normalized


# ---------------------------------------------------------------------------
# Draft construction and private persistence
# ---------------------------------------------------------------------------


def route_dir(env: Mapping[str, str] | None = None) -> str:
    """The private draft directory under the XDG CAO state root."""

    return os.path.join(state_dir(env), "intake-routes")


def draft_path(draft_id: str, env: Mapping[str, str] | None = None) -> str:
    return os.path.join(route_dir(env), _record_id(draft_id, "draft id") + ".json")


def draft_id_for(inputs: Mapping[str, Any]) -> str:
    """The deterministic draft id: a one-way digest of the immutable inputs."""

    return hashlib.sha256(canonical_json(inputs).encode("utf-8")).hexdigest()


def _immutable_inputs(
    packet: Mapping[str, Any],
    route: Mapping[str, Any],
    sources: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "packet_id": packet["packet_id"],
        "packet_fingerprint": packet["content_fingerprint"],
        "plan": route["plan"],
        "capability": route["capability"],
        "authority": route["authority"],
        "scope": route["scope"],
        "manifest": manifest,
        "sources": {
            label: {
                "verdict": evidence["verdict"],
                "head": evidence.get("head"),
                "digest": evidence.get("digest"),
            }
            for label, evidence in sources.items()
        },
    }


def _build_draft(
    packet: Mapping[str, Any],
    route: Mapping[str, Any],
    sources: Mapping[str, Any],
    manifest: Mapping[str, Any],
    registry_evidence: Mapping[str, Any],
    *,
    clock: Any,
) -> dict[str, Any]:
    inputs = _immutable_inputs(packet, route, sources, manifest)
    draft_id = draft_id_for(inputs)
    plan = route["plan"]
    capability = route["capability"]
    authority = route["authority"]
    scope = route["scope"]
    draft: dict[str, Any] = {
        "schema": DRAFT_SCHEMA,
        "record_type": RECORD_TYPE,
        "draft_id": draft_id,
        "status": "PROPOSED",
        "authority": "none",
        "dispatch": False,
        "requires_review": True,
        "created_at": utc_now(clock),
        "content_fingerprint": "",
        "packet": {
            "packet_id": packet["packet_id"],
            "content_fingerprint": packet["content_fingerprint"],
            "disposition": packet["disposition"]["value"],
        },
        "plan": {
            "plan_id": plan["plan_id"],
            "initiative": plan["initiative"],
            "objective": plan["objective"],
            "work_package": plan["work_package"],
            "owner": plan["owner"],
            "reviewer": plan["reviewer"],
            "accepted_at": plan["accepted_at"],
            "approval_digest": plan["approval_digest"],
        },
        "capability": {
            "required": capability["required"],
            "performer_kind": capability["performer_kind"],
        },
        "authority_grant": {
            "grantor": authority["grantor"],
            "grant_digest": authority["grant_digest"],
            "declared": True,
            "verified": True,
        },
        "scope": {
            "repo": scope["repo"],
            "domain": scope["domain"],
            "paths": list(scope["paths"]),
        },
        "manifest": dict(manifest),
        "provenance": {
            "evidence_cutoff": {
                "plan_head": sources["plan"].get("head"),
                "plan_commit_at": sources["plan"].get("commit_at"),
            },
            "sources": [dict(evidence) for evidence in sources.values()],
            "registry": {
                "verdict": registry_evidence["verdict"],
                "active_claims": list(registry_evidence.get("active_claims", [])),
                "reason": registry_evidence.get("reason"),
            },
        },
        "review": {
            "owner": plan["owner"],
            "reviewer": plan["reviewer"],
            "provider_authorized": False,
            "action": "review then 'cao team init --approved'",
        },
        "uncertainties": [
            _NOTE_NOT_AUTHORITY,
            _NOTE_OWNER_STEP,
            _NOTE_PROVIDER,
        ],
    }
    draft["content_fingerprint"] = _content_fingerprint(draft)
    return draft


def _content_fingerprint(draft: Mapping[str, Any]) -> str:
    content = {
        key: value
        for key, value in draft.items()
        if key not in {"draft_id", "created_at", "content_fingerprint"}
    }
    return fingerprint(content)


def _persist(draft: Mapping[str, Any], env: Mapping[str, str] | None) -> dict[str, Any]:
    name = draft["draft_id"] + ".json"
    payload = json.dumps(
        draft, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False
    )
    if len(payload.encode("utf-8")) > intake.MAX_RECORD_BYTES:
        raise CompileInvalid("compiled draft exceeds the private record bound")
    dir_fd = intake.open_private_dir(route_dir(env))
    try:
        created = intake.write_private_record(dir_fd, name, payload)
        data = intake.read_private_record(dir_fd, name)
    finally:
        os.close(dir_fd)
    if created:
        if data is None:
            raise CompileStateError("compiled draft failed its read-back check")
        stored = _verify_draft(data, draft["draft_id"])
        if stored != draft:
            raise CompileStateError("compiled draft failed its read-back check")
        return {"status": "ADMITTED", "draft": draft}
    if data is None:
        raise CompileUnavailable("an existing draft disappeared before read")
    stored = _verify_draft(data, draft["draft_id"])
    if stored["content_fingerprint"] == draft["content_fingerprint"]:
        return {"status": "DUPLICATE", "draft": stored}
    raise CompileConflict(
        "draft id already exists with different content; refusing to merge"
    )


# ---------------------------------------------------------------------------
# Strict draft verification and read surface
# ---------------------------------------------------------------------------


def _verify_draft(data: Any, expected_id: str) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise CompileStateError("stored draft is not a JSON object")
    if data.get("schema") != DRAFT_SCHEMA:
        raise CompileStateError("stored draft has an unknown schema")
    if set(data) != _DRAFT_KEYS:
        raise CompileStateError("stored draft has an unexpected shape")
    if data.get("record_type") != RECORD_TYPE:
        raise CompileStateError("stored record is not a route draft")
    if (data.get("status") != "PROPOSED" or data.get("authority") != "none"
            or data.get("dispatch") is not False
            or data.get("requires_review") is not True):
        raise CompileStateError("stored draft is not a proposed, non-authorizing record")
    try:
        declared = _record_id(data.get("draft_id"), "draft_id")
        stored_print = _bounded_str(
            data.get("content_fingerprint"), "content_fingerprint", 128,
            allow_empty=False,
        )
        _bounded_str(data.get("created_at"), "created_at", 64, allow_empty=False)
    except CompileInvalid as exc:
        raise CompileStateError("stored draft is malformed") from exc
    if declared != expected_id:
        raise CompileStateError("stored draft id does not match its file")
    manifest = data.get("manifest")
    if not isinstance(manifest, Mapping):
        raise CompileStateError("stored draft manifest is malformed")
    try:
        normalized = team.validate_spec(dict(manifest))
    except team.TeamError as exc:
        raise CompileStateError("stored draft manifest is not a valid team") from exc
    if normalized != dict(manifest):
        raise CompileStateError("stored draft manifest is not canonical")
    if _content_fingerprint(data) != stored_print:
        raise CompileStateError("stored draft content does not match its fingerprint")
    return dict(data)


def public_view(draft: Mapping[str, Any]) -> dict[str, Any]:
    """A bounded summary of one draft for listing (no manifest body)."""

    return {
        "draft_id": draft["draft_id"],
        "packet_id": draft["packet"]["packet_id"],
        "plan_id": draft["plan"]["plan_id"],
        "team_id": draft["manifest"]["team_id"],
        "status": draft["status"],
        "created_at": draft["created_at"],
    }


def list_drafts(env: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Bounded summaries of every stored draft, oldest first, fail-closed."""

    directory = route_dir(env)
    dir_fd = intake.open_private_dir_readonly(directory)
    if dir_fd is None:
        return []
    try:
        views: list[dict[str, Any]] = []
        for name in intake.list_private_names(dir_fd):
            if name.startswith(".") or not name.endswith(".json"):
                continue
            draft_id = name[: -len(".json")]
            if not _ID_RE.fullmatch(draft_id):
                raise CompileStateError("route draft directory holds an unexpected file")
            data = intake.read_private_record(dir_fd, name)
            if data is None:
                continue
            views.append(public_view(_verify_draft(data, draft_id)))
    finally:
        os.close(dir_fd)
    views.sort(key=lambda view: (view["created_at"], view["draft_id"]))
    return views


def read_draft(
    draft_id: str, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Read one full draft, or fail closed."""

    did = _record_id(draft_id, "draft id")
    dir_fd = intake.open_private_dir_readonly(route_dir(env))
    if dir_fd is None:
        raise CompileUnavailable("no route draft store exists")
    try:
        data = intake.read_private_record(dir_fd, did + ".json")
    finally:
        os.close(dir_fd)
    if data is None:
        raise CompileUnavailable("no draft with that id")
    return _verify_draft(data, did)


# ---------------------------------------------------------------------------
# The compile entry point
# ---------------------------------------------------------------------------


def compile_route(
    packet_id: str,
    route_file: str,
    *,
    env: Mapping[str, str] | None = None,
    dry_run: bool = False,
    clock: Any = None,
) -> dict[str, Any]:
    """Compile one ``PROPOSE_WORK`` triage packet into a reviewable draft.

    Reads the persisted B3 packet, the bounded route evidence and read-only
    Registry state; verifies each cited source against Git; and compiles one
    immutable ``cao.team.v1`` manifest. It starts nothing, claims nothing and,
    when ``dry_run`` is set, creates no state at all.
    """

    pid = _record_id(packet_id, "packet id")
    if not isinstance(route_file, str) or not route_file:
        raise CompileInvalid("route file must be a non-empty path")
    packet = intake_triage.read_packet(pid, env)
    if packet["disposition"]["value"] != "PROPOSE_WORK":
        raise CompileUnavailable(
            "only a PROPOSE_WORK triage packet may be routed for compilation"
        )
    document = intake.load_submission_file(route_file)
    route = _validate_route(document, packet)

    sources = {
        "plan": verify_source(route["plan"]["source"], "plan"),
        "capability": verify_source(route["capability"]["source"], "capability"),
        "authority": verify_source(route["authority"]["source"], "authority"),
    }
    failures = [
        f"{label}: {evidence['reason']}"
        for label, evidence in sources.items()
        if evidence["verdict"] != "SUPPORTED"
    ]
    if failures:
        raise CompileNeedsReview(
            "the route evidence is not objectively established", failures
        )

    if route["plan"]["accepted_at"] != sources["plan"]["commit_at"]:
        raise CompileNeedsReview(
            "the plan acceptance time does not match the immutable plan commit time",
            ["route.plan.accepted_at differs from the resolved commit time"],
        )

    if route["capability"]["performer_kind"] not in SUPPORTED_PERFORMER_KINDS:
        raise CompileUnavailable(
            "no existing Team Execution adapter supports performer kind "
            f"{route['capability']['performer_kind']!r}; the proposal stays "
            "advisory"
        )

    registry_evidence = _registry_overlaps(env, route["plan"]["objective"])
    if registry_evidence["verdict"] == "UNKNOWN":
        raise CompileNeedsReview(
            "the shared Work Registry could not be read",
            [registry_evidence.get("reason") or "registry read failed"],
        )
    if registry_evidence.get("overlaps"):
        overlap = registry_evidence["overlaps"][0]
        raise CompileUnavailable(
            "active Registry work overlaps this proposal "
            f"(work_key={overlap['work_key']}, score={overlap['score']})"
        )

    manifest = _compile_manifest(route)
    draft = _build_draft(
        packet, route, sources, manifest, registry_evidence, clock=clock
    )
    if dry_run:
        return {"schema": RESULT_SCHEMA, "status": "DRY_RUN",
                "persisted": False, "draft": draft}
    result = _persist(draft, env)
    return {
        "schema": RESULT_SCHEMA,
        "status": result["status"],
        "persisted": True,
        "duplicate": result["status"] == "DUPLICATE",
        "draft": result["draft"],
    }
