"""Standalone, authority-checked disposition control for paused CAO teams.

This module implements the four verbs behind the ``cao-disposition`` command:

``plan``
    Read one immutable PAUSED team, derive its true ``cao-team-<id>`` run and
    WRITE scope, probe live Git and runtime occupancy, reconcile the shared
    Registry read-only, and emit every conflicting worktree with its exact
    finding fingerprint and one canonical plan digest. It never writes.

``authorize``
    Load the one protected, root-owned disposition grant and bind it to the
    exact current plan. A CLI argument, an environment boolean, an in-process
    string or a caller-supplied document is never authentication: the grant is
    read descriptor-relative from a fixed system path with every component's
    owner and mode checked.

``apply``
    Re-plan live at the moment of action, revalidate the exact protected grant,
    refuse any active writer claim or live agent, and write every approved
    independent run-local disposition inside one ``BEGIN IMMEDIATE`` SQLite
    transaction. A failure rolls the whole batch back; an identical replay
    writes nothing. Historical reuse is never performed and the claim ledger is
    never altered.

``verify``
    Re-probe read-only, show what still blocks, show the provenance that
    resolved the rest, and report whether a separately governed writer
    dispatch could proceed. It never starts a team and never claims anything.

The ordinary CAO product CLI, the Registry, the team owner, the legacy intake
authority and the team-execution document are not modified by this lane.
Standard library only; no network, no shell command and no new ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone

from . import (
    git,
    intake_authority,
    product_config,
    product_daemon,
    product_driver,
    product_state,
    registry,
    state,
    team,
)

__all__ = [
    "PLAN_SCHEMA",
    "AUTHORIZE_SCHEMA",
    "APPLY_SCHEMA",
    "VERIFY_SCHEMA",
    "GRANT_SCHEMA",
    "GRANT_DIRECTORY",
    "GRANT_BASENAME",
    "ACTION",
    "DispositionError",
    "DispositionUnavailable",
    "DispositionRefused",
    "validate_grant",
    "plan",
    "authorize",
    "apply",
    "verify",
]

PLAN_SCHEMA = "cao.disposition.plan.v1"
AUTHORIZE_SCHEMA = "cao.disposition.authorize.v1"
APPLY_SCHEMA = "cao.disposition.apply.v1"
VERIFY_SCHEMA = "cao.disposition.verify.v1"

#: The one purpose-specific protected grant. It is deliberately a different
#: basename from the standing intake grant, so one host provision never
#: authorizes both concerns.
GRANT_SCHEMA = "cao.disposition.authority.v1"
GRANT_DIRECTORY = "/etc/cao"
GRANT_BASENAME = "disposition-authority.json"

#: The only action this command understands. There is no wildcard action.
ACTION = "run_local_independent"

#: The protected grant is small and bounded.
MAX_GRANT_BYTES = 64 * 1024
MAX_GRANT_WORKTREES = 128
MAX_GRANT_TTL_SECONDS = 30 * 24 * 60 * 60

#: Production trust anchors. A hermetic test may patch these module-level
#: constants; the command line never exposes them, so a caller cannot choose
#: the protected path or the UID it is checked against.
_GRANT_DIRECTORY = GRANT_DIRECTORY
_GRANT_BASENAME = GRANT_BASENAME
_GRANT_UID = 0
_TRUSTED_BASE = "/"

_IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}\Z")
_TEAM_ID = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")

_GRANT_KEYS = frozenset(
    {
        "schema",
        "team_id",
        "plan_digest",
        "manifest_digest",
        "operator",
        "expires_at",
        "revoked",
        "action",
        "scope",
        "worktrees",
    }
)
#: A grant must carry exactly one issue timestamp. ``granted_at`` is accepted
#: as the legacy spelling used by the sibling protected intake grant; it is
#: normalized to ``issued_at`` before validation so both cannot disagree.
_ISSUE_KEYS = frozenset({"issued_at", "granted_at"})

_NO_GRANT = (
    "no protected disposition-authority grant was established on this host; "
    "the operator must separately provision one root-owned file at "
    + GRANT_DIRECTORY
    + "/"
    + GRANT_BASENAME
    + " before apply, so it remains UNAVAILABLE"
)


class DispositionError(Exception):
    """Base class for an expected disposition refusal or unavailability."""

    code = "DISPOSITION_ERROR"
    status = "REFUSED"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class DispositionUnavailable(DispositionError):
    """A required external source could not be established."""

    code = "DISPOSITION_UNAVAILABLE"
    status = "UNAVAILABLE"


class DispositionRefused(DispositionError):
    """The request is unsafe, stale, unauthorized or ambiguous."""

    code = "DISPOSITION_REFUSED"
    status = "REFUSED"


# ---------------------------------------------------------------------------
# Small deterministic helpers
# ---------------------------------------------------------------------------


def _utc_now(value: object = None) -> datetime:
    """An aware UTC instant, or a refusal for an invalid injected clock."""

    if value is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise DispositionRefused("the clock must carry a timezone")
        return value.astimezone(timezone.utc).replace(microsecond=0)
    if isinstance(value, str):
        try:
            parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError as exc:
            raise DispositionRefused("the clock is not a UTC timestamp") from exc
        return parsed.replace(tzinfo=timezone.utc)
    raise DispositionRefused("the clock must be a UTC timestamp or datetime")


def _stamp(value: object, field: str) -> str:
    if type(value) is not str or not _STAMP.fullmatch(value):
        raise DispositionRefused(f"{field} must be a UTC timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise DispositionRefused(f"{field} must be a real UTC timestamp") from exc
    return value


def _ident(value: object, field: str) -> str:
    if type(value) is not str or not _IDENT.fullmatch(value):
        raise DispositionRefused(f"{field} must be a bounded identity")
    return value


def _team_ident(value: object) -> str:
    if type(value) is not str or not _TEAM_ID.fullmatch(value):
        raise DispositionRefused("team id must be a bounded lowercase identifier")
    return value


def _hex64(value: object, field: str) -> str:
    if type(value) is not str or not _HEX64.fullmatch(value):
        raise DispositionRefused(f"{field} must be 64 lowercase hex characters")
    return value


def _canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def _sha256(obj: object) -> str:
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def _norm_abs(path: object, field: str) -> str:
    if type(path) is not str or not os.path.isabs(path) or "\x00" in path:
        raise DispositionRefused(f"{field} must be an absolute path")
    normalized = os.path.normpath(path)
    if normalized != path:
        raise DispositionRefused(f"{field} must be normalized")
    real = os.path.realpath(path)
    if real != path:
        raise DispositionRefused(f"{field} must not traverse a symlink")
    return path


def _scope_paths(value: object) -> list[str]:
    if type(value) is not list or not value or len(value) > 128:
        raise DispositionRefused("grant scope.paths must be a non-empty bounded list")
    paths: list[str] = []
    for entry in value:
        if (type(entry) is not str or not entry or len(entry) > 1024
                or os.path.isabs(entry) or ".." in entry.split("/")
                or entry == "*"):
            raise DispositionRefused("grant scope.paths names an out-of-scope path")
        paths.append(entry)
    if len(set(paths)) != len(paths):
        raise DispositionRefused("grant scope.paths must be unique")
    return sorted(paths)


# ---------------------------------------------------------------------------
# Protected grant loading and validation
# ---------------------------------------------------------------------------


def _load_protected_grant() -> dict:
    """Read the fixed protected grant, or fail closed.

    The path and UID are module constants, never CLI input. An absent leaf is
    the honest ``UNAVAILABLE`` case; a symlinked, foreign-owned, mode-relaxed,
    oversized, duplicate-key or non-finite document is refused rather than
    rounded down to absent.
    """

    try:
        return intake_authority.read_protected_grant(
            directory=_GRANT_DIRECTORY,
            basename=_GRANT_BASENAME,
            uid=_GRANT_UID,
            trusted_base=_TRUSTED_BASE,
        )
    except intake_authority.AuthorityUnavailable as exc:
        raise DispositionUnavailable(_NO_GRANT) from exc
    except (intake_authority.AuthorityInvalid,
            intake_authority.AuthorityStateError) as exc:
        raise DispositionRefused(
            "the protected disposition authority is not a usable grant: "
            + str(exc),
            code="DISPOSITION_AUTHORITY_INVALID",
        ) from exc


def validate_grant(document: object, *, now: object = None) -> dict:
    """Validate one protected disposition grant, failing closed."""

    moment = _utc_now(now)
    if type(document) is not dict:
        raise DispositionRefused("the protected disposition grant must be an object")
    keys = set(document)
    if (not _GRANT_KEYS <= keys
            or keys - _GRANT_KEYS - _ISSUE_KEYS
            or len(keys & _ISSUE_KEYS) != 1):
        raise DispositionRefused(
            "the protected disposition grant has missing, ambiguous or "
            "unsupported field(s)")
    if document.get("schema") != GRANT_SCHEMA:
        raise DispositionRefused("the protected disposition grant has an unsupported schema")

    team_id = _team_ident(document.get("team_id"))
    plan_digest = _hex64(document.get("plan_digest"), "grant.plan_digest")
    manifest_digest = _hex64(document.get("manifest_digest"),
                             "grant.manifest_digest")
    operator = _ident(document.get("operator"), "grant.operator")

    issued_value = (document.get("issued_at") if "issued_at" in document
                    else document.get("granted_at"))
    issued_raw = _stamp(issued_value, "grant.issued_at")
    expires_raw = _stamp(document.get("expires_at"), "grant.expires_at")
    issued = datetime.strptime(issued_raw, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)
    expires = datetime.strptime(expires_raw, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)
    if expires <= issued:
        raise DispositionRefused("grant.expires_at must follow grant.issued_at")
    if (expires - issued).total_seconds() > MAX_GRANT_TTL_SECONDS:
        raise DispositionRefused(
            f"grant validity exceeds its bounded TTL of {MAX_GRANT_TTL_SECONDS} seconds"
        )
    if issued > moment:
        raise DispositionRefused("grant.issued_at is in the future")
    if expires <= moment:
        raise DispositionRefused("the protected disposition grant has expired")

    revoked = document.get("revoked")
    if type(revoked) is not bool:
        raise DispositionRefused("grant.revoked must be true or false")
    if revoked:
        raise DispositionRefused("the protected disposition grant is revoked")
    if document.get("action") != ACTION:
        raise DispositionRefused("grant.action is not the permitted disposition action")

    scope = document.get("scope")
    if type(scope) is not dict or set(scope) != {"repo", "paths"}:
        raise DispositionRefused("grant.scope must be an object of repo and paths")
    repo = _norm_abs(scope["repo"], "grant.scope.repo")
    paths = _scope_paths(scope["paths"])

    raw_worktrees = document.get("worktrees")
    if (type(raw_worktrees) is not list or not raw_worktrees
            or len(raw_worktrees) > MAX_GRANT_WORKTREES):
        raise DispositionRefused("grant.worktrees must be a non-empty bounded list")
    worktrees: list[dict] = []
    seen: set[tuple[str, str]] = set()
    seen_paths: set[str] = set()
    for entry in raw_worktrees:
        if type(entry) is not dict or set(entry) != {"worktree", "fingerprint"}:
            raise DispositionRefused(
                "grant.worktrees entries must carry exactly worktree and fingerprint"
            )
        worktree = _norm_abs(entry["worktree"], "grant.worktrees.worktree")
        fingerprint = _hex64(entry["fingerprint"],
                             "grant.worktrees.fingerprint")
        key = (worktree, fingerprint)
        if key in seen or worktree in seen_paths:
            raise DispositionRefused(
                "grant.worktrees must name each worktree and fingerprint at most once"
            )
        seen.add(key)
        seen_paths.add(worktree)
        worktrees.append({"worktree": worktree, "fingerprint": fingerprint})

    envelope = {
        "schema": GRANT_SCHEMA,
        "team_id": team_id,
        "plan_digest": plan_digest,
        "manifest_digest": manifest_digest,
        "operator": operator,
        "issued_at": issued_raw,
        "expires_at": expires_raw,
        "revoked": False,
        "action": ACTION,
        "scope": {"repo": repo, "paths": paths},
        "worktrees": sorted(worktrees, key=lambda item: item["worktree"]),
    }
    envelope["grant_fingerprint"] = _sha256(envelope)
    return envelope


def _authority_grant(now: object = None) -> dict:
    return validate_grant(_load_protected_grant(), now=now)


# ---------------------------------------------------------------------------
# Live runtime occupancy
# ---------------------------------------------------------------------------


def _runtime_observation(env: Mapping[str, str] | None) -> registry.RuntimeObservation:
    """Read the installed runtime's occupancy, or report UNAVAILABLE."""

    try:
        record = product_state.read_managed(env)
    except Exception as exc:  # noqa: BLE001 - an unreadable record is not empty
        raise DispositionUnavailable(
            "the managed runtime record could not be read") from exc
    if record is None:
        raise DispositionUnavailable(
            "the managed runtime has not been installed; live occupancy is "
            "unavailable")
    executable = record.get("path")
    if type(executable) is not str or not executable:
        raise DispositionUnavailable(
            "the managed runtime record has no installed path")
    if not os.path.isfile(executable):
        raise DispositionUnavailable(
            "the managed runtime executable is missing on disk")
    driver = product_driver.RuntimeDriver(executable=executable)
    occupancy = product_daemon.collect_occupancy(driver)
    if not occupancy.available:
        raise DispositionUnavailable(
            "live runtime occupancy is unavailable: "
            + (occupancy.reason or "unreadable"))
    return product_daemon.to_runtime_observation(occupancy)


# ---------------------------------------------------------------------------
# Team derivation and read-only planning
# ---------------------------------------------------------------------------


def _team_record(team_id: str, env: Mapping[str, str] | None) -> dict:
    try:
        return team._required(team_id, env)
    except team.TeamError as exc:
        raise DispositionRefused(
            "the requested team is not a readable immutable CAO team: "
            + str(exc)) from exc
    except (state.StateError, OSError) as exc:
        raise DispositionRefused(
            "the team checkpoint could not be read; the team is not usable") from exc


def _team_shape(record: Mapping) -> tuple[dict, str, list[str], list[dict], str]:
    """Return validated spec, run id, scope, WRITE tasks and the single repo."""

    team_id = record["team_id"]
    spec = record["spec"]
    summary = team.summary(record)
    if summary.get("status") != "PAUSED" or record.get("desired") != "paused":
        raise DispositionRefused(
            "only an immutable PAUSED team can be dispositioned; this team is "
            + str(summary.get("status")))
    for task in spec["tasks"]:
        slot = record["slots"].get(task["id"]) or {}
        if slot.get("status") not in ("pending", "complete"):
            raise DispositionRefused(
                "a PAUSED team may only hold pending or complete tasks; task "
                + task["id"] + " is " + str(slot.get("status")))

    repos = sorted({task["repo"] for task in spec["tasks"]})
    if len(repos) != 1:
        raise DispositionRefused(
            "this disposition command supports one repository per team; "
            "the manifest spans " + str(len(repos)))
    repo = repos[0]
    if not os.path.isdir(repo):
        raise DispositionRefused("the team repository does not exist as a directory")

    writes = [task for task in spec["tasks"] if task["mode"] == "write"]
    scope = sorted({path for task in writes for path in task["write_scope"]})
    if not writes or not scope:
        raise DispositionRefused(
            "the team manifest declares no WRITE task and scope to reconcile")
    return spec, "cao-team-" + team_id, scope, writes, repo


def _repo_tree(evidence: Mapping, repo: str) -> dict:
    worktrees = evidence.get("worktrees")
    if type(worktrees) is not dict:
        raise DispositionUnavailable("the live Git worktree probe is unavailable")
    items = worktrees.get("items")
    if type(items) is not list:
        raise DispositionUnavailable("the live Git worktree probe is unavailable")
    matches = [item for item in items
               if isinstance(item, Mapping) and item.get("path") == repo]
    if len(matches) != 1:
        raise DispositionRefused(
            "the team repository could not be identified exactly once among "
            "its live worktrees")
    tree = matches[0]
    if tree.get("detached") or tree.get("bare") or not tree.get("branch"):
        raise DispositionRefused(
            "the team repository must be an attached, non-bare checkout")
    if not git.valid_commit_id(tree.get("head")):
        raise DispositionUnavailable("the team repository HEAD is not readable")
    return tree


def _conflicts_for(
    findings: list[dict], writes: list[dict], run_id: str, repo: str,
    branch: str,
) -> list[dict]:
    """Every finding that would block at least one declared WRITE task."""

    selected: dict[tuple[str, str], dict] = {}
    for finding in findings:
        worktree = finding.get("worktree")
        if type(worktree) is not str or not worktree:
            if finding.get("unverifiable") or finding.get("kind") == registry.UNKNOWN:
                key = registry.finding_key(finding)
                selected[("", key)] = finding
            continue
        real = os.path.realpath(worktree)
        fingerprint = (finding.get("fingerprint")
                       or registry.finding_fingerprint(finding))
        for task in writes:
            spec = registry.ClaimSpec(
                run_id=run_id,
                work_key=task["id"],
                owner="cao-disposition-plan",
                mode=registry.WRITE,
                branch=branch,
                worktree=repo,
            )
            if registry._overlapping(
                    finding, spec, sorted(task["write_scope"]), repo):
                selected[(real, fingerprint)] = finding
                break
    return sorted(selected.values(), key=lambda item: (
        str(item.get("worktree") or ""), registry.finding_key(item)))


def _conflict_view(finding: dict) -> dict:
    worktree = finding.get("worktree") or ""
    fingerprint = finding.get("fingerprint") or registry.finding_fingerprint(finding)
    return {
        "worktree": worktree,
        "fingerprint": fingerprint,
        "finding_key": finding.get("finding_key") or registry.finding_key(finding),
        "kind": finding.get("kind", ""),
        "status": finding.get("status", ""),
        "head": finding.get("head", ""),
        "branch": finding.get("branch", ""),
        "worktree_branch": finding.get("worktree_branch", ""),
        "detached": bool(finding.get("detached")),
        "dirty_digest": finding.get("dirty_digest", ""),
        "dirty_digest_unknown": bool(finding.get("dirty_digest_unknown")),
        "status_codes": sorted(finding.get("status_codes") or []),
        "changes": sorted(finding.get("changes") or []),
        "committed": sorted(finding.get("committed") or []),
        "escaped": sorted(finding.get("escaped") or []),
        "touched": sorted(finding.get("touched") or []),
        "scope": sorted(finding.get("scope") or []),
        "components": sorted(finding.get("components") or []),
        "committed_unknown": bool(finding.get("committed_unknown")),
        "scope_unknown": bool(finding.get("scope_unknown")),
        "unverifiable": bool(finding.get("unverifiable")),
        "claim_id": finding.get("claim_id"),
        "run_id": finding.get("run_id", ""),
        "work_key": finding.get("work_key", ""),
        "owner": finding.get("owner", ""),
        "mode": finding.get("mode", ""),
        "agents": sorted(
            [
                {
                    "agent_id": item.get("agent_id", ""),
                    "worktree": item.get("worktree", ""),
                    "state": item.get("state", ""),
                    "session_id": item.get("session_id", ""),
                    "liveness": item.get("liveness", ""),
                }
                for item in (list(finding.get("agents") or [])
                             + list(finding.get("workers") or []))
            ],
            key=lambda item: (item["agent_id"], item["worktree"],
                              item["state"], item["liveness"]),
        ),
    }


def _active_claims(conn, identity: str) -> list[dict]:
    try:
        rows = registry.list_active_claims(conn)
    except Exception as exc:  # noqa: BLE001 - an unreadable ledger is not empty
        raise DispositionUnavailable(
            "the shared Registry claim ledger could not be read") from exc
    selected = []
    for row in rows:
        try:
            same = git.normalize_repo(row.get("repo") or "") == identity
        except Exception:  # noqa: BLE001 - a hostile stored path is not a match
            same = False
        if same:
            selected.append({
                "claim_id": row.get("claim_id"),
                "run_id": row.get("run_id", ""),
                "work_key": row.get("work_key", ""),
                "issue": row.get("issue", ""),
                "owner": row.get("owner", ""),
                "mode": row.get("mode", ""),
                "branch": row.get("branch", ""),
                "worktree": row.get("worktree", ""),
                "claimed_at": row.get("claimed_at", ""),
            })
    return sorted(selected, key=lambda item: int(item.get("claim_id") or 0))


def _live_agents(observation: registry.RuntimeObservation) -> list[dict]:
    out = []
    for agent in observation.agents:
        liveness = registry.agent_liveness(agent.state)
        if liveness == registry.UNKNOWN_LIVENESS:
            raise DispositionRefused(
                "a live runtime occupant reports an unreadable state; no "
                "disposition can be authorized")
        out.append({
            "agent_id": agent.agent_id,
            "worktree": agent.worktree,
            "state": agent.state,
            "session_id": agent.session_id,
            "liveness": liveness,
        })
    return sorted(out, key=lambda item: (
        item["agent_id"], item["worktree"], item["state"]))


def _preflight_entries(
    conn, record: Mapping, run_id: str, repo: str, branch: str,
    observation: registry.RuntimeObservation,
) -> list[dict]:
    """Read-only Registry preflight for any already-proposed WRITE task."""

    entries = []
    for task in record["spec"]["tasks"]:
        if task["mode"] != "write":
            continue
        slot = record["slots"][task["id"]]
        work_key = team._execution_id(
            record["team_id"], task["id"], slot["phase"], slot["round"],
            slot.get("retry_no", 0))
        work = registry.get_work(conn, run_id, work_key, "")
        if work is None:
            entries.append({"task_id": task["id"], "work_key": work_key,
                            "status": "NOT_PROPOSED", "errors": []})
            continue
        spec = registry.ClaimSpec(
            run_id=run_id, work_key=work_key, owner="cao-disposition-preflight",
            mode=registry.WRITE, branch=branch, worktree=repo)
        try:
            result = registry.preflight(conn, spec, observation)
        except Exception as exc:  # noqa: BLE001 - preflight never writes
            raise DispositionUnavailable(
                "shared Registry preflight could not read live evidence") from exc
        entries.append({
            "task_id": task["id"],
            "work_key": work_key,
            "status": str(result.get("status") or "UNKNOWN"),
            "errors": [str(item) for item in (result.get("errors") or [])][:8],
        })
    return entries


def _plan_digest(payload: Mapping) -> str:
    return _sha256(payload)


def _build_plan(
    team_id: str,
    *,
    env: Mapping[str, str] | None,
    observation: registry.RuntimeObservation | None,
    db: str | None,
    conn=None,
) -> dict:
    team_id = _team_ident(team_id)
    record = _team_record(team_id, env)
    spec, run_id, scope, writes, repo = _team_shape(record)
    repo = os.path.realpath(repo)

    if observation is None:
        observation = _runtime_observation(env)
    if type(observation) is not registry.RuntimeObservation or not observation.available:
        raise DispositionUnavailable("live runtime occupancy is unavailable")

    try:
        db_path = db or product_config.registry_db_path(env)
    except (product_config.ProductConfigError, state.StateError) as exc:
        raise DispositionUnavailable(
            "the registered Registry path could not be established") from exc
    owns_conn = conn is None
    if owns_conn:
        try:
            conn = registry.connect_readonly(db_path)
        except (registry.RegistryError, OSError, sqlite3.Error) as exc:
            raise DispositionUnavailable(
                "the shared Registry could not be opened read-only") from exc
    try:
        evidence = registry.gather_evidence(repo, observation, repo)
        report = registry.reconcile(conn, evidence)
        if not report.get("ok"):
            raise DispositionUnavailable(
                "shared Registry reconciliation could not read a trustworthy "
                "live state")
        if report.get("occupancy_unknown"):
            raise DispositionUnavailable("runtime occupancy is unverifiable")
        tree = _repo_tree(evidence, repo)
        identity = report.get("identity") or ""
        if not identity:
            raise DispositionUnavailable("the repository identity is unverifiable")

        findings = list(report.get("findings") or [])
        if any(finding.get("kind") == registry.UNKNOWN
               or finding.get("unverifiable") for finding in findings):
            raise DispositionRefused(
                "at least one live finding is UNKNOWN or unverifiable; scope "
                "that could not be read is never dispositioned")

        conflicts = _conflicts_for(findings, writes, run_id, repo,
                                   tree.get("branch", ""))
        views = [_conflict_view(finding) for finding in conflicts]
        if len({item["worktree"] for item in views}) != len(views):
            raise DispositionRefused(
                "two different findings describe one worktree; the conflict is "
                "ambiguous")
        agents = _live_agents(observation)
        claims = _active_claims(conn, identity)
        preflight_entries = _preflight_entries(
            conn, record, run_id, repo, tree.get("branch", ""), observation)

        manifest_digest = team.fingerprint(spec)
        digest_payload = {
            "schema": PLAN_SCHEMA,
            "team_id": team_id,
            "run_id": run_id,
            "manifest_digest": manifest_digest,
            "repo": {
                "path": repo,
                "identity": identity,
                "head": tree.get("head", ""),
                "branch": tree.get("branch", ""),
                "detached": bool(tree.get("detached")),
            },
            "scope": scope,
            "baseline": report.get("baseline", ""),
            "conflicts": [
                {
                    "worktree": item["worktree"],
                    "fingerprint": item["fingerprint"],
                    "finding_key": item["finding_key"],
                    "kind": item["kind"],
                    "status": item["status"],
                    "head": item["head"],
                    "branch": item["branch"],
                    "worktree_branch": item["worktree_branch"],
                    "detached": item["detached"],
                    "dirty_digest": item["dirty_digest"],
                    "status_codes": item["status_codes"],
                    "claim_id": item["claim_id"],
                    "owner": item["owner"],
                    "mode": item["mode"],
                }
                for item in views
            ],
            "active_claims": [
                {
                    "claim_id": item["claim_id"],
                    "run_id": item["run_id"],
                    "work_key": item["work_key"],
                    "issue": item["issue"],
                    "owner": item["owner"],
                    "mode": item["mode"],
                    "branch": item["branch"],
                    "worktree": item["worktree"],
                }
                for item in claims
            ],
            "agents": [
                {
                    "agent_id": item["agent_id"],
                    "worktree": item["worktree"],
                    "state": item["state"],
                    "session_id": item["session_id"],
                    "liveness": item["liveness"],
                }
                for item in agents
            ],
        }
        digest = _plan_digest(digest_payload)
        active_writers = [item for item in claims
                          if item["mode"] in (registry.WRITE,
                                              registry.EXCLUSIVE)]
        active_live = [item for item in agents
                       if item["liveness"] == "ACTIVE"]
        blocking_preflight = [
            item for item in preflight_entries
            if item["status"] not in ("PREFLIGHT", "NOT_PROPOSED", "CLAIMED")
        ]
        preflight = {
            "status": ("CONFLICT" if views or active_writers or active_live
                       or blocking_preflight else "PREFLIGHT"),
            "entries": preflight_entries,
            "blocking_worktrees": [item["worktree"] for item in views],
            "active_writer_claims": [item["claim_id"] for item in active_writers],
            "live_agent_count": len(active_live),
        }
        return {
            "schema": PLAN_SCHEMA,
            "status": "PLAN",
            "team_id": team_id,
            "run_id": run_id,
            "manifest_digest": manifest_digest,
            "repo": digest_payload["repo"],
            "scope": scope,
            "baseline": digest_payload["baseline"],
            "conflicts": views,
            "active_claims": claims,
            "live_agents": agents,
            "runtime": {"available": True, "agents": len(agents)},
            "preflight": preflight,
            "plan_digest": digest,
        }
    finally:
        if owns_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Grant binding
# ---------------------------------------------------------------------------


def _binding_digest(envelope: Mapping, plan: Mapping) -> str:
    return _sha256({
        "plan_digest": plan["plan_digest"],
        "grant_fingerprint": envelope["grant_fingerprint"],
    })


def _approved(
    envelope: Mapping, plan: Mapping,
) -> list[dict]:
    """The current findings the protected grant explicitly approves."""

    if envelope.get("team_id") != plan.get("team_id"):
        raise DispositionRefused("the protected grant names a different team")
    if envelope.get("manifest_digest") != plan.get("manifest_digest"):
        raise DispositionRefused(
            "the protected grant was issued for a different team manifest")
    if envelope.get("plan_digest") != plan.get("plan_digest"):
        raise DispositionRefused(
            "the protected grant is stale: the current plan digest changed")
    if envelope.get("scope") != {"repo": plan["repo"]["path"],
                                 "paths": list(plan["scope"])}:
        raise DispositionRefused(
            "the protected grant scope does not exactly match the current "
            "team WRITE scope")

    by_worktree = {}
    for finding in plan["conflicts"]:
        worktree = finding.get("worktree")
        if type(worktree) is not str or not worktree:
            raise DispositionRefused(
                "a conflicting finding has no identifiable worktree")
        by_worktree[worktree] = finding
    if len(by_worktree) != len(plan["conflicts"]):
        raise DispositionRefused("the conflicting worktree set is ambiguous")

    approved = []
    for pair in envelope["worktrees"]:
        finding = by_worktree.get(pair["worktree"])
        if finding is None:
            raise DispositionRefused(
                "the protected grant approves a worktree that is not a current "
                "conflict")
        if finding.get("fingerprint") != pair["fingerprint"]:
            raise DispositionRefused(
                "the protected grant fingerprint no longer matches the live "
                "finding")
        if finding.get("claim_id") is not None or finding.get("owner"):
            raise DispositionRefused(
                "the protected grant approves work still owned by an open "
                "Registry claim")
        if (finding.get("kind") == registry.UNKNOWN
                or finding.get("unverifiable")
                or finding.get("dirty_digest_unknown")):
            raise DispositionRefused(
                "the protected grant approves an UNKNOWN finding")
        approved.append(finding)
    return approved


def _authority_string(envelope: Mapping, plan: Mapping) -> str:
    return "cao-disposition:%s:%s" % (
        envelope["operator"], _binding_digest(envelope, plan))


def _rationale(envelope: Mapping, plan: Mapping) -> str:
    return (
        "independent run-local disposition; action=%s; operator=%s; "
        "plan_digest=%s; manifest_digest=%s; grant_sha256=%s; binding_sha256=%s"
        % (ACTION, envelope["operator"], plan["plan_digest"],
           plan["manifest_digest"], envelope["grant_fingerprint"],
           _binding_digest(envelope, plan))
    )


# ---------------------------------------------------------------------------
# Public verbs
# ---------------------------------------------------------------------------


def plan(
    team_id: str,
    *,
    env: Mapping[str, str] | None = None,
    observation: registry.RuntimeObservation | None = None,
    db: str | None = None,
    now: object = None,
) -> dict:
    """Read-only plan. ``observation`` exists for hermetic integration tests;
    the command line never accepts it and always probes the installed runtime.
    """

    _utc_now(now)
    return _build_plan(team_id, env=env, observation=observation, db=db)


def authorize(
    team_id: str,
    *,
    env: Mapping[str, str] | None = None,
    observation: registry.RuntimeObservation | None = None,
    db: str | None = None,
    now: object = None,
) -> dict:
    """Bind one externally authenticated protected grant to the current plan.

    There is no authority parameter: the only source is the fixed protected
    grant, read by the existing descriptor-relative primitive. This verb never
    writes.
    """

    moment = _utc_now(now)
    envelope = _authority_grant(moment)
    payload = _build_plan(team_id, env=env, observation=observation, db=db)
    approved = _approved(envelope, payload)
    return {
        "schema": AUTHORIZE_SCHEMA,
        "status": "AUTHORIZED",
        "team_id": payload["team_id"],
        "run_id": payload["run_id"],
        "operator": envelope["operator"],
        "manifest_digest": payload["manifest_digest"],
        "plan_digest": payload["plan_digest"],
        "grant_fingerprint": envelope["grant_fingerprint"],
        "binding_digest": _binding_digest(envelope, payload),
        "issued_at": envelope["issued_at"],
        "expires_at": envelope["expires_at"],
        "scope": envelope["scope"],
        "approved": [
            {"worktree": item["worktree"], "fingerprint": item["fingerprint"],
             "kind": item["kind"], "status": item["status"]}
            for item in approved
        ],
        "not_approved_conflicts": [
            {"worktree": item["worktree"], "fingerprint": item["fingerprint"]}
            for item in payload["conflicts"]
            if item not in approved
        ],
    }


def _ensure_apply_preconditions(payload: Mapping) -> None:
    writers = [item for item in payload.get("active_claims") or []
               if item.get("mode") in (registry.WRITE, registry.EXCLUSIVE)]
    if writers:
        raise DispositionRefused(
            "an active Registry WRITE/EXCLUSIVE claim still owns this "
            "repository; disposition is refused")
    if any(item.get("liveness") == "ACTIVE"
           for item in payload.get("live_agents") or []):
        raise DispositionRefused(
            "a live runtime agent still occupies this repository; disposition "
            "is refused")
    if payload.get("runtime", {}).get("available") is not True:
        raise DispositionUnavailable("live runtime occupancy is unavailable")


def _insert_independent(
    conn, run_id: str, finding: Mapping, authority: str, rationale: str,
) -> tuple[int, str]:
    """Insert one independent disposition inside the caller's transaction."""

    if not run_id.strip():
        raise registry.RegistryError("run id is required to record a disposition")
    if finding.get("kind") == registry.UNKNOWN or finding.get("unverifiable") \
            or finding.get("committed_unknown") \
            or finding.get("scope_unknown") \
            or finding.get("dirty_digest_unknown"):
        raise registry.RegistryError(
            "UNKNOWN findings cannot be dispositioned")
    if not authority.strip() or not rationale.strip():
        raise registry.RegistryError(
            "an independent disposition requires provenance")
    key = finding.get("finding_key") or registry.finding_key(finding)
    fingerprint = registry.finding_fingerprint(finding)
    stamp = registry.now()
    cur = conn.execute(
        "INSERT INTO dispositions (run_id, finding_key, fingerprint, "
        "disposition, authority, rationale, target_owner, created_at, "
        "verified_historical) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, key, fingerprint, registry.DISPOSITION_INDEPENDENT,
         authority.strip(), rationale.strip(), "", stamp, 0))
    registry.record_observation(conn, run_id, "", registry.DISPOSITION_KIND, {
        "finding_key": key,
        "fingerprint": fingerprint,
        "kind": finding.get("kind", ""),
        "disposition": registry.DISPOSITION_INDEPENDENT,
        "authority": authority.strip(),
        "rationale": rationale.strip(),
        "target_owner": "",
    })
    return int(cur.lastrowid), stamp


def apply(team_id: str) -> dict:
    """Apply one atomic batch using the process configuration and live probes.

    Effectful calls accept no clock, runtime observation or configuration
    overrides; hermetic tests patch the underlying sources instead.
    """

    envelope = _authority_grant()
    try:
        db_path = product_config.registry_db_path()
    except (product_config.ProductConfigError, state.StateError) as exc:
        raise DispositionUnavailable(
            "the registered Registry path could not be established") from exc
    initial = _build_plan(team_id, env=None, observation=None, db=db_path)
    _approved(envelope, initial)
    _ensure_apply_preconditions(initial)
    authority = _authority_string(envelope, initial)
    rationale = _rationale(envelope, initial)

    if not os.path.isfile(db_path):
        raise DispositionUnavailable(
            "the shared Registry does not exist; apply never creates a ledger")
    try:
        conn = registry.connect(db_path)
    except (registry.RegistryError, OSError, sqlite3.Error) as exc:
        raise DispositionUnavailable(
            "the shared Registry could not be opened for the atomic batch") from exc
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-read the protected grant under the write lock, so a grant
            # revoked or replaced after the first read cannot authorize this
            # batch.
            current = _authority_grant()
            if current["grant_fingerprint"] != envelope["grant_fingerprint"]:
                raise DispositionRefused(
                    "the protected disposition grant changed before the batch")
            # Re-plan while holding the write lock. No caller-supplied plan or
            # digest is trusted between authorization and the batch.
            live = _build_plan(team_id, env=None, observation=None,
                               db=db_path, conn=conn)
            if live["plan_digest"] != initial["plan_digest"]:
                raise DispositionRefused(
                    "the live plan changed between authorization and apply; "
                    "the protected grant is stale")
            approved = _approved(envelope, live)
            _ensure_apply_preconditions(live)
            results = []
            inserted = 0
            for finding in approved:
                key = finding.get("finding_key") or registry.finding_key(finding)
                fingerprint = registry.finding_fingerprint(finding)
                existing = conn.execute(
                    "SELECT disp_id, run_id, finding_key, fingerprint, "
                    "disposition, authority, rationale, created_at, "
                    "verified_historical FROM dispositions WHERE run_id=? AND "
                    "finding_key=? AND fingerprint=? AND disposition=? AND "
                    "verified_historical=0 ORDER BY disp_id DESC LIMIT 1",
                    (live["run_id"], key, fingerprint,
                     registry.DISPOSITION_INDEPENDENT),
                ).fetchone()
                if existing is not None:
                    results.append({
                        "worktree": finding["worktree"],
                        "finding_key": key,
                        "fingerprint": fingerprint,
                        "disp_id": existing["disp_id"],
                        "disposition": registry.DISPOSITION_INDEPENDENT,
                        "authority": existing["authority"],
                        "rationale": existing["rationale"],
                        "created_at": existing["created_at"],
                        "verified_historical": False,
                        "replayed": True,
                    })
                    continue
                disp_id, created_at = _insert_independent(
                    conn, live["run_id"], finding, authority, rationale)
                inserted += 1
                results.append({
                    "worktree": finding["worktree"],
                    "finding_key": key,
                    "fingerprint": fingerprint,
                    "disp_id": disp_id,
                    "disposition": registry.DISPOSITION_INDEPENDENT,
                    "authority": authority,
                    "rationale": rationale,
                    "created_at": created_at,
                    "verified_historical": False,
                    "replayed": False,
                })
            # Planning and inserts may outlive the grant's validity.
            current = _authority_grant()
            if current["grant_fingerprint"] != envelope["grant_fingerprint"]:
                raise DispositionRefused(
                    "the protected disposition grant changed before commit")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {
        "schema": APPLY_SCHEMA,
        "status": "APPLIED" if inserted else "REPLAYED",
        "team_id": initial["team_id"],
        "run_id": initial["run_id"],
        "operator": envelope["operator"],
        "plan_digest": initial["plan_digest"],
        "manifest_digest": initial["manifest_digest"],
        "grant_fingerprint": envelope["grant_fingerprint"],
        "binding_digest": _binding_digest(envelope, initial),
        "authority": authority,
        "applied": results,
        "inserted": inserted,
        "replayed": inserted == 0,
        "claims_changed": 0,
    }


def verify(
    team_id: str,
    *,
    env: Mapping[str, str] | None = None,
    observation: registry.RuntimeObservation | None = None,
    db: str | None = None,
    now: object = None,
) -> dict:
    """Read-only re-probe: remaining blocks, resolved provenance, readiness."""

    _utc_now(now)
    payload = _build_plan(team_id, env=env, observation=observation, db=db)
    try:
        db_path = db or product_config.registry_db_path(env)
    except (product_config.ProductConfigError, state.StateError) as exc:
        raise DispositionUnavailable(
            "the registered Registry path could not be established") from exc
    try:
        conn = registry.connect_readonly(db_path)
    except (registry.RegistryError, OSError, sqlite3.Error) as exc:
        raise DispositionUnavailable(
            "the shared Registry could not be opened read-only") from exc
    try:
        try:
            rows = conn.execute(
                "SELECT disp_id, run_id, finding_key, fingerprint, disposition, "
                "authority, rationale, created_at, verified_historical "
                "FROM dispositions WHERE run_id=? ORDER BY disp_id",
                (payload["run_id"],)).fetchall()
        except sqlite3.Error as exc:
            raise DispositionUnavailable(
                "the shared Registry disposition ledger is unreadable") from exc
        all_rows = [dict(row) for row in rows]
        remaining = []
        resolved = []
        for finding in payload["conflicts"]:
            try:
                match = registry.find_disposition(
                    conn, payload["run_id"], finding)
            except (registry.RegistryError, sqlite3.Error) as exc:
                raise DispositionUnavailable(
                    "the shared Registry disposition lookup is unreadable") from exc
            if match is None:
                remaining.append(finding)
                continue
            provenance = None
            try:
                provenance = registry.released_source_provenance(conn, finding)
            except Exception:  # noqa: BLE001 - a read-only verification never raises
                provenance = None
            resolved.append({
                "worktree": finding["worktree"],
                "legacy_status": finding["status"],
                "fingerprint": finding["fingerprint"],
                "disposition": match.get("disposition", ""),
                "authority": match.get("authority", ""),
                "rationale": match.get("rationale", ""),
                "created_at": match.get("created_at", ""),
                "run_id": match.get("run_id", ""),
                "verified_historical": bool(match.get("verified_historical")),
                "released_source": provenance,
            })
    finally:
        conn.close()

    active_writers = [item for item in payload["active_claims"]
                      if item["mode"] in (registry.WRITE, registry.EXCLUSIVE)]
    live_agents = [item for item in payload["live_agents"]
                   if item["liveness"] == "ACTIVE"]
    ready = not remaining and not active_writers and not live_agents
    return {
        "schema": VERIFY_SCHEMA,
        "status": "VERIFY",
        "team_id": payload["team_id"],
        "run_id": payload["run_id"],
        "manifest_digest": payload["manifest_digest"],
        "plan_digest": payload["plan_digest"],
        "repo": payload["repo"],
        "scope": payload["scope"],
        "conflicts": payload["conflicts"],
        "remaining": remaining,
        "resolved": resolved,
        "dispositions": all_rows,
        "active_writer_claims": [item["claim_id"] for item in active_writers],
        "live_agents": live_agents,
        "preflight": {
            "status": ("CONFLICT" if remaining or active_writers or live_agents
                       else "PREFLIGHT"),
            "remaining": [item["worktree"] for item in remaining],
        },
        "ready_for_separate_writer_dispatch": ready,
        "writer_dispatch": "SEPARATE_GOVERNED_AUTHORITY_REQUIRED",
        "auto_started": False,
        "claim_written": False,
    }
