"""Standing Intake Authority (B8): the external operator trust boundary.

B6 can derive one gated READ handoff plan and B7 can persist and reconcile one
prequalified READ intent, but both deliberately ship **no** operator authority:
a CLI flag, a process-environment value, a JSON file and ``authenticated:true``
are caller-controlled and are never proof of who approved. This module supplies
the one missing thing -- a verified, **in-process** authority adapter -- and it
anchors it to a genuine external trust boundary: a single, **operator-owned,
root-owned** grant file in a fixed protected system directory, opened
descriptor-relative with every path component ``fstat``-checked (owner, mode,
no symlink). CAO itself can never write that file and a hash or mode CAO
controls is not proof, so an authority can only exist when the operator
separately provisioned the protected grant.

Load-bearing rules this module enforces:

* **Authentication is external.** The grant is read from a fixed protected
  directory, validated against ``uid`` ownership and non-writable mode for every
  component, opened ``O_NOFOLLOW`` and read through the pinned descriptor, so a
  symlink, a TOCTOU swap or a CAO-user-writable path fails closed. Local XDG
  state, the environment, argv, a task prompt, an arbitrary user-owned JSON file,
  an ``authenticated:true`` bit, a spec digest and a model response are all
  never proof.
* **The grant binds dynamically.** The adapter returns the B6 grant bound to the
  *exact* derived plan digest of the plan it is asked about, and it refuses any
  plan whose submission, source repository/ref, model or privacy qualification
  is outside the approved envelope, or whose envelope is expired or revoked.
* **A bounded quota, no new ledger.** Consumption is derived from the existing
  work artifacts (the dispatched CAO READ teams and the Work Registry claims
  they hold), never from a second authority ledger; when that evidence cannot be
  read completely the adapter fails closed.
* **No new authority is ever created or installed.** CAO never writes the
  protected grant and never fabricates one. When no valid grant exists on the
  host, ``standing_authority`` reports ``UNAVAILABLE``, the daemon supplies no
  adapter and the first dispatch is held -- honestly.

Standard library only; no third-party dependency, no network and no credential.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from . import intake_handoff, intake_queue, product_config, registry, team
from .evidence import fingerprint, utc_now

__all__ = [
    "AUTHORITY_SCHEMA",
    "STATUS_SCHEMA",
    "DEFAULT_GRANT_DIR",
    "DEFAULT_GRANT_BASENAME",
    "MAX_GRANT_BYTES",
    "MAX_SUBMISSIONS",
    "MAX_DISPATCHES",
    "AuthorityError",
    "AuthorityInvalid",
    "AuthorityUnavailable",
    "AuthorityRefused",
    "AuthorityStateError",
    "validate_grant",
    "read_protected_grant",
    "consumption",
    "OperatorAuthority",
    "standing_authority",
    "status",
]

#: The schema of the protected, operator-authored grant document.
AUTHORITY_SCHEMA = "cao.intake.authority.v1"
#: The read-only status projection of the standing authority (persists nothing).
STATUS_SCHEMA = "cao.intake.authority.status.v1"

#: The fixed protected system directory an operator provisions. The daemon
#: never creates it and never writes there; only root can.
DEFAULT_GRANT_DIR = "/etc/cao"
#: The single grant basename inside that directory.
DEFAULT_GRANT_BASENAME = "intake-authority.json"
#: The protected grant is small and bounded.
MAX_GRANT_BYTES = 64 * 1024
#: The standing authority covers at most this many submissions when it enumerates.
MAX_SUBMISSIONS = 256
#: The standing authority may dispatch at most this many READ executions.
MAX_DISPATCHES = 1000
#: A grant must carry an *effective*, bounded validity interval: this is the
#: maximum expiry window (measured from granted_at) any grant may declare, so
#: a de facto unbounded grant is refused like a malformed one.
MAX_GRANT_TTL_SECONDS = 30 * 24 * 60 * 60
#: Consumption inspects at most this many team checkpoints before failing
#: closed; an excessive store is never silently truncated.
MAX_TEAM_CHECKS = 4096

_IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}\Z")
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_MODEL = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:@+/#-]{0,150}\Z"
)
_TEAM_ID = re.compile(r"intake-read-[0-9a-f]{16}\Z")

_GRANT_KEYS = frozenset(
    {
        "schema",
        "operator",
        "owner",
        "role",
        "model",
        "privacy_qualified",
        "scope",
        "source",
        "submissions",
        "max_dispatches",
        "granted_at",
        "expires_at",
        "revoked",
    }
)

_NO_GRANT = (
    "no protected standing-authority grant was established on this host; the "
    "operator must separately provision a root-owned grant in the fixed "
    "protected directory before any first dispatch, so it is held"
)
_PROTECTED_INTEGRATION = (
    "provision exactly one root-owned grant file in " + DEFAULT_GRANT_DIR +
    " (a fixed protected system directory), owned by uid 0 with no group/other "
    "write access, and never CAO-writable; CAO never creates or installs it"
)


class AuthorityError(Exception):
    """Base class for the B8 standing intake authority."""

    code = "INTAKE_AUTHORITY_ERROR"


class AuthorityInvalid(AuthorityError):
    """The protected grant document is malformed; nothing is authorized."""

    code = "INTAKE_AUTHORITY_INVALID"


class AuthorityUnavailable(AuthorityError):
    """No protected grant could be established on this host."""

    code = "INTAKE_AUTHORITY_UNAVAILABLE"


class AuthorityRefused(AuthorityError):
    """A grant, scope, model, quota or provenance check denied the dispatch."""

    code = "INTAKE_AUTHORITY_REFUSED"


class AuthorityStateError(AuthorityError):
    """The protected path exists but is not a usable grant.

    A foreign-owned, mode-relaxed, symlinked, oversized or unreadable protected
    path is refused rather than rounded down to "absent".
    """

    code = "INTAKE_AUTHORITY_STATE"


# ---------------------------------------------------------------------------
# Strict, descriptor-relative reader of the protected grant
# ---------------------------------------------------------------------------


def _reject_constant(token: str) -> Any:
    raise AuthorityInvalid("the protected grant must not contain non-finite numbers")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AuthorityInvalid("the protected grant must not repeat a JSON key")
        result[key] = value
    return result


def _strict_json(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AuthorityInvalid("the protected grant is not valid UTF-8") from exc
    try:
        return json.loads(
            text, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except AuthorityInvalid:
        raise
    except ValueError as exc:
        raise AuthorityInvalid("the protected grant is not valid JSON") from exc


def _open_owned_dir_chain(
    directory: str, uid: int, trusted_base: str
) -> int:
    """Open ``directory`` descriptor-relative, checking every component below
    ``trusted_base`` for owner ``uid``, directory type and no group/other write.

    ``trusted_base`` defaults to the filesystem root, so production validates
    each real component (``/etc`` and ``/etc/cao``). It exists only because a
    hermetic test cannot chown an ancestor of a temp directory to root; the
    production caller never overrides it. ``O_NOFOLLOW`` on every component
    turns a symlinked component into a refusal, and the returned descriptor is
    pinned, so a swap after validation cannot redirect the read.
    """

    if not isinstance(directory, str) or not os.path.isabs(directory):
        raise AuthorityInvalid("the protected directory must be an absolute path")
    if os.path.normpath(directory) != directory:
        raise AuthorityInvalid("the protected directory must be normalized")
    base = os.path.normpath(trusted_base) if isinstance(trusted_base, str) else "/"
    if not os.path.isabs(base):
        raise AuthorityInvalid("the trusted base must be an absolute path")
    prefix = base if base.endswith("/") else base + "/"
    if directory != base and not directory.startswith(prefix):
        raise AuthorityInvalid("the protected directory is outside the trusted base")
    relative = directory[len(base):].lstrip("/")

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(base, flags)
    except OSError as exc:
        raise AuthorityStateError(
            "the protected base could not be opened; refusing rather than "
            "trusting an unreadable path"
        ) from exc
    try:
        base_info = os.fstat(fd)
        if not stat.S_ISDIR(base_info.st_mode):
            raise AuthorityStateError("the protected base is not a directory")
        for part in (relative.split("/") if relative else []):
            if part in ("", "."):
                continue
            if part == "..":
                raise AuthorityInvalid("the protected directory contains '..'")
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError as exc:
                # A genuinely absent protected path is the honest "not
                # provisioned" case, not a tampered one.
                raise AuthorityUnavailable(_NO_GRANT) from exc
            except OSError as exc:
                # O_NOFOLLOW makes a symlinked component fail here (ELOOP).
                raise AuthorityStateError(
                    f"the protected path component {part!r} could not be opened "
                    "as a real directory"
                ) from exc
            info = os.fstat(child)
            if not stat.S_ISDIR(info.st_mode):
                os.close(child)
                raise AuthorityStateError(
                    f"the protected path component {part!r} is not a directory"
                )
            if info.st_uid != uid:
                os.close(child)
                raise AuthorityStateError(
                    f"the protected path component {part!r} is not owned by the "
                    "trusted operator"
                )
            if info.st_mode & 0o022:
                os.close(child)
                raise AuthorityStateError(
                    f"the protected path component {part!r} is writable by group "
                    "or other and is not a trustworthy boundary"
                )
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _read_owned_file(dir_fd: int, name: str, uid: int) -> bytes:
    """Read one bounded root-owned regular file from a pinned descriptor."""

    if (not isinstance(name, str) or not name or "/" in name
            or name in (".", "..")):
        raise AuthorityInvalid("the grant basename must be a single component")
    try:
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
    except FileNotFoundError as exc:
        raise AuthorityUnavailable(_NO_GRANT) from exc
    except OSError as exc:
        raise AuthorityStateError(
            "the protected grant could not be opened as a real file"
        ) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise AuthorityStateError("the protected grant is not a regular file")
        if info.st_uid != uid:
            raise AuthorityStateError(
                "the protected grant is not owned by the trusted operator"
            )
        if info.st_mode & 0o022:
            raise AuthorityStateError(
                "the protected grant is writable by group or other and is not "
                "proof"
            )
        if info.st_size > MAX_GRANT_BYTES:
            raise AuthorityStateError("the protected grant exceeds its size bound")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_GRANT_BYTES:
                raise AuthorityStateError("the protected grant exceeds its size bound")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_protected_grant(
    *,
    directory: str = DEFAULT_GRANT_DIR,
    basename: str = DEFAULT_GRANT_BASENAME,
    uid: int = 0,
    trusted_base: str = "/",
) -> dict[str, Any]:
    """Read the one protected, operator-owned grant, or fail closed.

    A genuinely absent leaf grant is :class:`AuthorityUnavailable` (the honest
    "not provisioned" case, which holds the first dispatch). Every other defect
    -- a symlinked or foreign-owned component, a group/other-writable mode, an
    unreadable path, an oversized file, malformed JSON, a duplicate key or a
    non-finite number -- raises rather than being rounded down to absent.
    """

    if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
        raise AuthorityInvalid("uid must be a non-negative integer")
    dir_fd = _open_owned_dir_chain(directory, uid, trusted_base)
    try:
        raw = _read_owned_file(dir_fd, basename, uid)
    finally:
        os.close(dir_fd)
    return _strict_json(raw)


# ---------------------------------------------------------------------------
# Grant validation
# ---------------------------------------------------------------------------


def _ident(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENT.fullmatch(value):
        raise AuthorityInvalid(f"{field} must be a bounded identity")
    return value


def _stamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _STAMP.fullmatch(value):
        raise AuthorityInvalid(f"{field} must be a UTC timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise AuthorityInvalid(f"{field} must be a real UTC timestamp") from exc
    return value


def _scope_paths(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 128:
        raise AuthorityInvalid("grant scope.paths must be a bounded list")
    paths: list[str] = []
    for entry in value:
        if (not isinstance(entry, str) or not entry or len(entry) > 1024
                or os.path.isabs(entry) or ".." in entry.split("/")):
            raise AuthorityInvalid("grant scope.paths names an out-of-scope path")
        paths.append(entry)
    return paths


def validate_grant(document: Any) -> dict[str, Any]:
    """Validate one protected grant document, failing closed on any defect."""

    if not isinstance(document, Mapping):
        raise AuthorityInvalid("the protected grant must be a JSON object")
    if set(document) != _GRANT_KEYS:
        raise AuthorityInvalid(
            "the protected grant has missing or unsupported field(s)"
        )
    if document.get("schema") != AUTHORITY_SCHEMA:
        raise AuthorityInvalid("the protected grant has an unsupported schema")

    operator = _ident(document.get("operator"), "grant.operator")
    owner = _ident(document.get("owner"), "grant.owner")
    role = _ident(document.get("role"), "grant.role")

    model = document.get("model")
    if (not isinstance(model, str) or not _MODEL.fullmatch(model)
            or ".." in model or "//" in model):
        raise AuthorityInvalid("grant.model must be an exact provider/model id")

    if document.get("privacy_qualified") is not True:
        raise AuthorityInvalid(
            "grant.privacy_qualified must be true; an unqualified provider is "
            "never authorized for the data"
        )

    scope = document.get("scope")
    if not isinstance(scope, Mapping) or set(scope) != {"repo", "paths"}:
        raise AuthorityInvalid("grant.scope must be an object of repo and paths")
    repo = scope["repo"]
    if (not isinstance(repo, str) or not os.path.isabs(repo)
            or os.path.normpath(repo) != repo):
        raise AuthorityInvalid(
            "grant.scope.repo must be a normalized absolute checkout path"
        )
    paths = _scope_paths(scope["paths"])

    source = document.get("source")
    if not isinstance(source, Mapping) or set(source) != {"ref"}:
        raise AuthorityInvalid("grant.source must be an object with a ref")
    ref = source["ref"]
    if ref is not None:
        from . import git  # local: the ref validator is a bounded Git probe
        if not isinstance(ref, str) or not (
            git.valid_commit_id(ref) or git.valid_short_ref(ref)
        ):
            raise AuthorityInvalid("grant.source.ref must be a ref name or commit id")

    submissions_raw = document.get("submissions")
    submissions: list[str] | None
    if submissions_raw is None:
        submissions = None
    else:
        if (not isinstance(submissions_raw, list)
                or len(submissions_raw) > MAX_SUBMISSIONS):
            raise AuthorityInvalid("grant.submissions must be a bounded list or null")
        submissions = []
        for entry in submissions_raw:
            if not isinstance(entry, str) or not _HEX64.fullmatch(entry):
                raise AuthorityInvalid(
                    "grant.submissions entries must be 64 lowercase hex ids"
                )
            submissions.append(entry)
        if len(set(submissions)) != len(submissions):
            raise AuthorityInvalid("grant.submissions entries must be unique")

    max_dispatches = document.get("max_dispatches")
    if (isinstance(max_dispatches, bool) or not isinstance(max_dispatches, int)
            or not 1 <= max_dispatches <= MAX_DISPATCHES):
        raise AuthorityInvalid(
            f"grant.max_dispatches must be an integer within [1, {MAX_DISPATCHES}]"
        )

    granted_at = _stamp(document.get("granted_at"), "grant.granted_at")
    expires_raw = document.get("expires_at")
    if expires_raw is None:
        raise AuthorityInvalid(
            "grant.expires_at is mandatory; a standing authority grant must "
            "carry an effective bounded validity interval")
    expires_at = _stamp(expires_raw, "grant.expires_at")
    if expires_at <= granted_at:
        raise AuthorityInvalid("grant.expires_at precedes granted_at")
    granted_dt = datetime.strptime(granted_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)
    expires_dt = datetime.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)
    if (expires_dt - granted_dt).total_seconds() > MAX_GRANT_TTL_SECONDS:
        raise AuthorityInvalid(
            f"grant validity exceeds its bounded TTL of "
            f"{MAX_GRANT_TTL_SECONDS} seconds")

    revoked = document.get("revoked")
    if not isinstance(revoked, bool):
        raise AuthorityInvalid("grant.revoked must be true or false")

    envelope = {
        "operator": operator,
        "owner": owner,
        "role": role,
        "model": model,
        "privacy_qualified": True,
        "scope": {"repo": repo, "paths": paths},
        "source": {"ref": ref},
        "submissions": submissions,
        "max_dispatches": max_dispatches,
        "granted_at": granted_at,
        "expires_at": expires_at,
        "revoked": revoked,
    }
    envelope["grant_fingerprint"] = fingerprint(envelope)
    return envelope


# ---------------------------------------------------------------------------
# Consumption: the existing work artifacts, never a new authority ledger
# ---------------------------------------------------------------------------


def _covered_submission(
    team_id: str, spec: Mapping[str, Any], envelope: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
) -> str | None:
    """The submission a B6-derived READ audit team id and manifest cover.

    The team id ``intake-read-<submission[:16]>`` and the immutable manifest
    prompt's full submission id must agree, so a team can only be attributed to
    the submission it actually audits. A foreign or forged team is never counted.

    A Batch B2d team cites only the one-way ``synthetic-`` alias in its prompt
    (the privacy-preserving form of the same governed READ audit), so when the
    raw admission id is absent the team is attributed from the governor's own
    durable handoff-plan binding instead -- never from a caller field and never
    by guessing.
    """

    tasks = spec.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return None
    for task in tasks:
        if (not isinstance(task, Mapping) or task.get("mode") != "read"
                or task.get("write_scope")):
            return None
    found: str | None = None
    for task in tasks:
        prompt = str(task.get("prompt") or "") if isinstance(task, Mapping) else ""
        for token in re.findall(r"[0-9a-f]{64}", prompt):
            if "intake-read-" + token[:16] == team_id:
                found = token
    if found is None:
        found = _alias_covered_submission(team_id, tasks, env)
    if found is None:
        return None
    submissions = envelope.get("submissions")
    if submissions is not None and found not in submissions:
        return None
    return found


def _alias_covered_submission(
    team_id: str, tasks: list[Any], env: Mapping[str, str] | None
) -> str | None:
    """The submission of an alias-citing READ team, from durable evidence.

    Only a manifest that cites a bounded ``synthetic-`` alias is eligible, and
    its submission is then resolved from the persisted handoff-plan binding. A
    missing or malformed binding raises so consumption fails closed rather than
    undercounting the shared quota.
    """

    cited = any(
        re.search(r"synthetic-[0-9a-f]{40}", str(task.get("prompt") or ""))
        for task in tasks if isinstance(task, Mapping)
    )
    if not cited:
        return None
    resolved = intake_handoff.plan_submission_for_team(team_id, env)
    if resolved is None:
        # The only path that creates an alias-citing READ team also persists its
        # durable handoff-plan binding; a team without one is partial evidence,
        # so consumption fails closed instead of undercounting the shared quota.
        raise intake_handoff.HandoffStateError(
            "an alias-citing READ team carries no durable handoff-plan binding")
    return resolved


def consumption(
    *, env: Mapping[str, str] | None, envelope: Mapping[str, Any]
) -> dict[str, Any]:
    """The dispatch units the standing authority has already consumed.

    Consumption is re-derived from the **existing** durable artifacts -- the
    dispatched, admission-bound READ teams this envelope covers and their active
    Work Registry claims -- never from a second authority ledger. Any
    unreadable team store or Registry is ``UNKNOWN``, and the caller then fails
    closed rather than guessing a remaining budget.
    """

    base: dict[str, Any] = {
        "verdict": "SUPPORTED", "consumed": 0, "teams": [], "claims": [],
        "reason": None,
    }
    try:
        listed = team.list_teams(env=env)
    except Exception:  # noqa: BLE001 - unreadable team state is never zero
        return {**base, "verdict": "UNKNOWN",
                "reason": "local CAO team state could not be read"}
    counted: list[str] = []
    # Every record is inspected. Silent truncation would round an exhausted
    # quota down to a fresh one, so an overlarge store fails closed instead of
    # being cut to a fixed prefix.
    if len(listed) > MAX_TEAM_CHECKS:
        return {**base, "verdict": "UNKNOWN",
                "reason": "the local CAO team store exceeds its inspection "
                          f"bound of {MAX_TEAM_CHECKS} records"}
    for view in listed:
        if not isinstance(view, Mapping):
            continue
        if view.get("status") == "CORRUPT":
            return {**base, "verdict": "UNKNOWN",
                    "reason": "a local CAO team checkpoint is corrupt"}
        team_id = view.get("team_id")
        if not isinstance(team_id, str) or not _TEAM_ID.fullmatch(team_id):
            continue
        try:
            record = team._required(team_id, env)
        except Exception:  # noqa: BLE001 - unverifiable is never counted
            return {**base, "verdict": "UNKNOWN",
                    "reason": "a bound CAO team checkpoint could not be read"}
        spec = record.get("spec")
        if not isinstance(spec, Mapping):
            return {**base, "verdict": "UNKNOWN",
                    "reason": "a bound CAO team has no immutable manifest"}
        try:
            covered = _covered_submission(team_id, spec, envelope, env)
        except Exception:  # noqa: BLE001 - a partial read is never a fresh quota
            return {**base, "verdict": "UNKNOWN",
                    "reason": "a durable handoff-plan binding could not be read"}
        if covered is not None:
            counted.append(team_id)

    claims: list[dict[str, Any]] = []
    try:
        ledger = product_config.registry_db_path(env)
    except Exception:  # noqa: BLE001 - an unresolvable Registry is UNKNOWN
        return {**base, "verdict": "UNKNOWN",
                "reason": "the local Work Registry location could not be resolved"}
    # Missing, symlinked, corrupt or unreadable Registry evidence is never a
    # fresh quota: consumption is only ever derived from *complete*, existing
    # durable evidence, so any gap yields UNKNOWN and the caller fails closed.
    try:
        ledger_info = os.lstat(ledger)
    except OSError as exc:
        return {**base, "verdict": "UNKNOWN",
                "reason": "the local Work Registry does not exist or cannot "
                          "be listed; quota evidence is incomplete"}
    if stat.S_ISLNK(ledger_info.st_mode):
        return {**base, "verdict": "UNKNOWN",
                "reason": "the local Work Registry is a symlinked path"}
    if not stat.S_ISREG(ledger_info.st_mode):
        return {**base, "verdict": "UNKNOWN",
                "reason": "the local Work Registry is not a regular database"}
    try:
        conn = registry.connect_readonly(ledger)
    except Exception:  # noqa: BLE001 - an unreadable Registry is UNKNOWN
        return {**base, "verdict": "UNKNOWN",
                "reason": "the local Work Registry could not be read"}
    try:
        for claim in registry.list_active_claims(conn):
            if not isinstance(claim, Mapping):
                continue
            run_id = str(claim.get("run_id") or "")
            if run_id.startswith("cao-team-") and run_id[len("cao-team-"):] in counted:
                claims.append({
                    "run_id": run_id[:64],
                    "work_key": str(claim.get("work_key") or "")[:64],
                    "mode": str(claim.get("mode") or "")[:16],
                })
    except Exception:  # noqa: BLE001 - an unreadable Registry is UNKNOWN
        return {**base, "verdict": "UNKNOWN",
                "reason": "the local Work Registry claims could not be read"}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - closing is best-effort
            pass

    return {
        **base,
        "consumed": len(counted),
        "teams": sorted(counted),
        "claims": claims,
    }


# ---------------------------------------------------------------------------
# The verified in-process authority adapter
# ---------------------------------------------------------------------------


class OperatorAuthority:
    """A verified, in-process operator authority bound to one standing envelope.

    Instances are callables with the exact B6 ``operator_authority`` signature.
    The call returns the grant bound to the *exact* derived plan digest, or a
    non-authenticated record when any envelope check fails; the last evaluation
    is kept for an actionable reason.
    """

    #: A marker so the existing queue never confuses this with a caller object.
    cao_authority = True

    def __init__(
        self, envelope: Mapping[str, Any], *, env: Mapping[str, str] | None = None,
        clock: Any = None,
    ) -> None:
        self._envelope = dict(envelope)
        self._env = env
        self._clock = clock
        self.last: dict[str, Any] | None = None

    @property
    def envelope(self) -> dict[str, Any]:
        return dict(self._envelope)

    def evaluate(
        self, *, submission_id: str, plan: Mapping[str, Any],
        env: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """The full verdict for one plan, with an actionable refusal code."""

        envelope = self._envelope

        def refuse(code: str, reason: str) -> dict[str, Any]:
            return {"verdict": "REFUSED", "code": code, "reason": reason,
                    "operator": envelope["operator"], "grant": None}

        if envelope.get("revoked"):
            return refuse("authority_revoked",
                          "the standing authority grant was revoked by the operator")
        now = utc_now(self._clock)
        expires_at = envelope.get("expires_at")
        if expires_at is not None and now > expires_at:
            return refuse(
                "authority_expired",
                f"the standing authority grant expired at {expires_at}")

        binding = plan.get("binding")
        if not isinstance(binding, Mapping):
            return refuse("authority_binding", "the handoff plan has no binding")
        bound_submission = binding.get("submission_id")
        if bound_submission != submission_id:
            return refuse(
                "authority_binding",
                "the handoff plan's submission does not match the authority request")

        submissions = envelope.get("submissions")
        if submissions is not None and submission_id not in submissions:
            return refuse(
                "authority_submission_out_of_scope",
                "the submission is not enumerated by the standing authority grant")

        if binding.get("read_only") is not True:
            return refuse("authority_not_read_only",
                          "the handoff plan is not a READ-only execution")

        source = binding.get("source")
        if not isinstance(source, Mapping):
            return refuse("authority_source", "the handoff plan binds no source")
        if source.get("repo") != envelope["scope"]["repo"]:
            return refuse(
                "authority_source_mismatch",
                "the handoff source repository is not the granted checkout")
        if source.get("ref") != envelope["source"]["ref"]:
            return refuse(
                "authority_source_ref_mismatch",
                "the handoff source ref is not the granted ref")

        try:
            config = product_config.load_config(product_config.config_path(env))
            policy = product_config.team_model_policy(config)
        except product_config.ProductConfigError as exc:
            return refuse("authority_model_policy",
                          f"the model allowlist could not be read ({exc})")
        if policy is None:
            return refuse(
                "authority_model_policy_missing",
                "no configured model allowlist exists, so new automatic "
                "authority is refused until the operator configures one")
        if not policy.permits(envelope["model"]):
            return refuse(
                "authority_model_not_allowlisted",
                "the granted model is not on the configured model allowlist")

        evidence = consumption(env=env, envelope=envelope)
        if evidence["verdict"] != "SUPPORTED":
            return refuse(
                "authority_quota_evidence_incomplete",
                "complete quota evidence is unavailable: "
                + str(evidence.get("reason") or "unknown"))
        if evidence["consumed"] >= envelope["max_dispatches"]:
            return refuse(
                "authority_quota_exhausted",
                "the standing authority dispatch quota is exhausted")

        bound = intake_handoff._binding_digest(plan)
        grant = {
            "plan_digest": bound,
            "read_only": True,
            "owner": envelope["owner"],
            "scope": {
                "repo": envelope["scope"]["repo"],
                "paths": list(envelope["scope"]["paths"]),
            },
            "role": envelope["role"],
            "model": envelope["model"],
            "privacy_qualified": True,
            "granted_at": envelope["granted_at"],
        }
        return {
            "verdict": "SUPPORTED",
            "code": None,
            "reason": "the protected standing authority granted this exact "
                      "scoped READ execution",
            "operator": envelope["operator"],
            "grant": grant,
            "consumed": evidence["consumed"],
            "max_dispatches": envelope["max_dispatches"],
        }

    def __call__(
        self, *, submission_id: str, plan: Mapping[str, Any],
        env: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]:
        result = self.evaluate(
            submission_id=submission_id, plan=plan,
            env=env if env is not None else self._env)
        self.last = result
        if result["verdict"] != "SUPPORTED":
            return {"authenticated": False, "reason": result.get("code")}
        return {
            "authenticated": True,
            "operator": result["operator"],
            "grant": result["grant"],
        }


# ---------------------------------------------------------------------------
# The standing-authority record the daemon consumes
# ---------------------------------------------------------------------------


def _load(
    *, env: Mapping[str, str] | None, clock: Any, uid: int, directory: str,
    basename: str, trusted_base: str,
) -> dict[str, Any]:
    try:
        raw = read_protected_grant(
            directory=directory, basename=basename, uid=uid,
            trusted_base=trusted_base)
    except AuthorityUnavailable as exc:
        return {"verdict": "UNAVAILABLE", "code": "authority_unavailable",
                "reason": str(exc), "envelope": None, "adapter": None,
                "integration": _PROTECTED_INTEGRATION}
    except AuthorityError as exc:
        return {"verdict": "REFUSED", "code": exc.code, "reason": str(exc),
                "envelope": None, "adapter": None,
                "integration": _PROTECTED_INTEGRATION}
    try:
        envelope = validate_grant(raw)
    except AuthorityError as exc:
        return {"verdict": "REFUSED", "code": exc.code, "reason": str(exc),
                "envelope": None, "adapter": None}
    # Effective validity is resolved *before* any adapter or envelope is
    # released. A revoked, expired or future-dated grant is REFUSED here so the
    # pipeline enqueues nothing under it; the adapter applies the same checks
    # again at each dispatch as defense in depth.
    now = utc_now(clock)
    if envelope.get("revoked"):
        return {"verdict": "REFUSED", "code": "authority_revoked",
                "reason": "the standing authority grant is revoked by the "
                          "operator",
                "envelope": None, "adapter": None}
    if envelope["granted_at"] > now:
        return {"verdict": "REFUSED", "code": "authority_not_yet_valid",
                "reason": "the standing authority grant begins in the future "
                          f"at {envelope['granted_at']}",
                "envelope": None, "adapter": None}
    if now > envelope["expires_at"]:
        return {"verdict": "REFUSED", "code": "authority_expired",
                "reason": "the standing authority grant expired at "
                          f"{envelope['expires_at']}",
                "envelope": None, "adapter": None}
    adapter = OperatorAuthority(envelope, env=env, clock=clock)
    return {
        "verdict": "SUPPORTED",
        "code": None,
        "reason": "a protected, operator-owned standing authority grant is present",
        "envelope": envelope,
        "adapter": adapter,
        "integration": None,
    }


def standing_authority(
    env: Mapping[str, str] | None = None,
    clock: Any = None,
    *,
    uid: int = 0,
    directory: str = DEFAULT_GRANT_DIR,
    basename: str = DEFAULT_GRANT_BASENAME,
    trusted_base: str = "/",
) -> dict[str, Any]:
    """Resolve the standing authority from the protected host grant.

    Returns a record with ``verdict``, ``code``, ``reason``, the validated
    ``envelope`` and the in-process ``adapter`` (``None`` unless ``SUPPORTED``).
    ``uid`` and ``trusted_base`` exist for a hermetic host-shaped test; the
    production default requires uid 0 and validates every real component.
    """

    return _load(env=env, clock=clock, uid=uid, directory=directory,
                 basename=basename, trusted_base=trusted_base)


def status(
    env: Mapping[str, str] | None = None,
    clock: Any = None,
    *,
    uid: int = 0,
    directory: str = DEFAULT_GRANT_DIR,
    basename: str = DEFAULT_GRANT_BASENAME,
    trusted_base: str = "/",
) -> dict[str, Any]:
    """A read-only projection of the standing authority; persists nothing.

    Unlike :func:`standing_authority` it never builds a dispatch adapter, so the
    CLI can report the protected grant honestly without constructing authority.
    """

    record = _load(env=env, clock=clock, uid=uid, directory=directory,
                   basename=basename, trusted_base=trusted_base)
    envelope = record.get("envelope")
    consumption_evidence: dict[str, Any] | None = None
    if envelope is not None:
        consumption_evidence = consumption(env=env, envelope=envelope)
    now = utc_now(clock)
    return {
        "schema": STATUS_SCHEMA,
        "verdict": record["verdict"],
        "code": record["code"],
        "reason": record["reason"],
        "required_integration": record.get("integration"),
        "now": now,
        "envelope": (
            None if envelope is None else
            {
                "operator": envelope["operator"],
                "owner": envelope["owner"],
                "role": envelope["role"],
                "model": envelope["model"],
                "privacy_qualified": envelope["privacy_qualified"],
                "scope": dict(envelope["scope"]),
                "source": dict(envelope["source"]),
                "submissions": envelope["submissions"],
                "max_dispatches": envelope["max_dispatches"],
                "granted_at": envelope["granted_at"],
                "expires_at": envelope["expires_at"],
                "revoked": envelope["revoked"],
                "grant_fingerprint": envelope["grant_fingerprint"],
            }
        ),
        "consumption": consumption_evidence,
        "authority": "standing" if envelope is not None else "none",
        "dispatch": False,
        "advisory": True,
        "egress": "none",
    }
