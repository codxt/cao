"""Portable Work Registry with evidence-gated writes.

The registry is a coordination ledger, not a file lock and not canonical
knowledge. Claims record intended ownership; they do not replace separate Git
worktrees, scope ownership, or the accountable supervisor's acceptance.

Design decisions that keep the guard deterministic and portable:

* Identity is exact. A claim's identity is ``(run, work, owner)`` per issue.
  Only an exact match resumes; there is no fuzzy or prefix matching.
* Repository identity is the normalized Git *common dir* (falling back to the
  resolved path outside Git), so a checkout and every linked worktree of one
  repository are one repository.
* Scope is normalized before comparison: repo-relative path prefixes and
  case-folded component ids.
* Acquisition is atomic. Every claim runs its whole check-then-insert inside
  one ``BEGIN IMMEDIATE`` transaction, so exactly one of N concurrent
  conflicting claimants wins.
* Conflicts are repository-wide, not run-wide or owner-wide.
* Fails closed. Empty scope, absolute paths, ``..`` escapes, unknown modes,
  unproposed work and unknown dependencies all refuse rather than guess.
* No expiry. Claims never lapse on their own; only an explicit ``release``
  ends one.

Live evidence is supplied, never fabricated. Git is probed here with
read-only probes (:mod:`cao.git`); occupancy is supplied by the caller through
the explicit :class:`RuntimeObservation` interface. The core makes no runtime
assumption and never invents an observation: a missing observation is
``UNKNOWN`` and a required-source failure refuses a write. A probe that fails
is unknown, never "clean".

A WRITE or EXCLUSIVE claim is therefore gated: the ledger verdict first (a
ledger refusal names the holding claim and is more precise than anything live
evidence could add), then the live gate over a claim that would actually be
granted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from urllib.parse import quote

from . import git

__all__ = [
    "READ", "WRITE", "EXCLUSIVE", "MODES",
    "RegistryError", "RegistryCorruptError",
    "RuntimeObservation", "AgentObservation",
    "ClaimSpec",
    "connect", "connect_readonly", "default_db_path",
    "propose",
    "claim", "preflight", "release", "handoff",
    "evaluate", "write_gate", "reconcile", "gather_evidence",
    "list_active_claims", "list_works", "get_work", "get_open_claim",
    "record_disposition", "find_disposition", "list_dispositions",
    "record_historical_disposition", "released_source_provenance",
    "finding_key", "finding_fingerprint", "stamp_findings",
    "objective_tokens", "lexical_similarity", "probable_overlaps",
    "record_observation", "list_observations",
    "CLAIMED", "RESUMED", "PREFLIGHT", "CONFLICT", "JOIN_EXISTING",
    "BLOCKED", "ERROR", "RELEASED", "PROPOSED", "UNCHANGED", "AMENDED",
    "NEEDS_RECONCILIATION", "NEEDS_SCOPE", "HANDED_OFF", "RESOLVED",
    "DISCOVERED_UNREGISTERED", "SCOPE_DRIFT", "STALE_DIRTY", "STALE_CLEAN",
    "UNKNOWN", "LIVE", "RELEASED_UNMERGED",
]

READ = "READ"
WRITE = "WRITE"
EXCLUSIVE = "EXCLUSIVE"
MODES = (READ, WRITE, EXCLUSIVE)

# Stable uppercase outcome tokens. These are part of the contract: callers
# branch on them, so never rename one in place.
CLAIMED = "CLAIMED"
RESUMED = "RESUMED"
PREFLIGHT = "PREFLIGHT"
CONFLICT = "CONFLICT"
JOIN_EXISTING = "JOIN_EXISTING"
BLOCKED = "BLOCKED"
ERROR = "ERROR"
RELEASED = "RELEASED"
PROPOSED = "PROPOSED"
UNCHANGED = "UNCHANGED"
AMENDED = "AMENDED"
NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"
NEEDS_SCOPE = "NEEDS_SCOPE"
HANDED_OFF = "HANDED_OFF"
RESOLVED = "RESOLVED"

# Stable uppercase finding kinds, part of the persisted contract.
DISCOVERED_UNREGISTERED = "DISCOVERED_UNREGISTERED"
SCOPE_DRIFT = "SCOPE_DRIFT"
STALE_CLEAN = "STALE_CLEAN"
STALE_DIRTY = "STALE_DIRTY"
UNKNOWN = "UNKNOWN"
LIVE = "LIVE"

# Diagnostic sub-status for a DISCOVERED_UNREGISTERED finding that the ledger
# proves is the clean preserved-unmerged leftover of a completed, released
# claim. It is a classification, not a waiver: the finding keeps its blocking
# kind until an explicit, fingerprint-bound historical disposition clears it.
RELEASED_UNMERGED = "released_unmerged_source"

# The one persisted reconciliation record kind: a whole current-state snapshot
# that a later reconciliation replaces wholesale.
RECONCILIATION = "RECONCILIATION"

# Persisted observation kinds for movements and decisions.
DISPOSITION_KIND = "DISPOSITION"
HANDOFF_KIND = "HANDOFF"

FINDING_KINDS = (DISCOVERED_UNREGISTERED, SCOPE_DRIFT, STALE_CLEAN, STALE_DIRTY,
                 UNKNOWN, LIVE)

# Disposition vocabulary. Smallest taxonomy that separates the genuinely
# different decisions. Only ``historical`` and ``independent`` answer "this is
# not contention"; ``join`` and ``handoff`` are decisions to ACT and keep
# blocking until the action has happened.
DISPOSITION_HISTORICAL = "historical"
DISPOSITION_INDEPENDENT = "independent"
DISPOSITION_JOIN = "join"
DISPOSITION_HANDOFF = "handoff"
DISPOSITIONS = (DISPOSITION_HISTORICAL, DISPOSITION_INDEPENDENT,
                DISPOSITION_JOIN, DISPOSITION_HANDOFF)
CLEARING_DISPOSITIONS = (DISPOSITION_HISTORICAL, DISPOSITION_INDEPENDENT)

APP_DIR = "cao-work-registry"
DB_NAME = "registry.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS works (
    run_id     TEXT NOT NULL,
    work_key   TEXT NOT NULL,
    issue      TEXT NOT NULL DEFAULT '',
    repo       TEXT NOT NULL,
    paths      TEXT NOT NULL DEFAULT '',
    components TEXT NOT NULL DEFAULT '',
    objective  TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, work_key, issue)
);

CREATE TABLE IF NOT EXISTS claims (
    claim_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,
    work_key   TEXT NOT NULL,
    issue      TEXT NOT NULL DEFAULT '',
    owner      TEXT NOT NULL,
    mode       TEXT NOT NULL,
    branch     TEXT NOT NULL DEFAULT '',
    worktree   TEXT NOT NULL DEFAULT '',
    claimed_at TEXT NOT NULL,
    released_at TEXT,
    outcome     TEXT,
    released_head TEXT NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS claims_open_identity
    ON claims (run_id, work_key, issue, owner) WHERE released_at IS NULL;

CREATE INDEX IF NOT EXISTS claims_open_run ON claims (run_id, released_at);

CREATE TABLE IF NOT EXISTS dependencies (
    run_id     TEXT NOT NULL,
    work_key   TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    PRIMARY KEY (run_id, work_key, depends_on)
);

CREATE TABLE IF NOT EXISTS observations (
    obs_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    work_key    TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS observations_run ON observations (run_id, work_key);

-- An explicit supervisor disposition about one observed finding. The
-- fingerprint binds the decision to the evidence it was made against: if the
-- HEAD, branch, worktree, status codes, content digest or touched scope change,
-- the fingerprint changes and the disposition stops applying. Nothing expires
-- by wall clock; only new evidence withdraws it.
CREATE TABLE IF NOT EXISTS dispositions (
    disp_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    finding_key  TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    disposition  TEXT NOT NULL,
    authority    TEXT NOT NULL,
    rationale    TEXT NOT NULL DEFAULT '',
    target_owner TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    -- Durable proof that this decision was made through the evidence-checked
    -- recorder, which re-verified the released source against the ledger. Only
    -- that path sets it to 1; a generic caller-attributed disposition is always
    -- 0 and stays run-local. A later, independent run may reuse a verified
    -- historical decision, but never a caller's own claim.
    verified_historical INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS dispositions_lookup
    ON dispositions (run_id, finding_key, fingerprint);
"""

REQUIRED_TABLES = ("runs", "works", "claims", "observations", "dispositions",
                   "dependencies")


class RegistryError(Exception):
    """Raised for a caller mistake that must fail closed."""


class RegistryCorruptError(RegistryError):
    """Raised when persisted registry bytes cannot be read as their own shape.

    A corrupt record is never returned as an absent or empty value: the caller
    is told which record is unreadable so the failure cannot masquerade as
    "nothing was recorded".
    """

    code = "REGISTRY_CORRUPT"


# --------------------------------------------------------------------------
# Time and connection
# --------------------------------------------------------------------------

def now() -> str:
    """UTC timestamp, second resolution."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_db_path() -> str:
    """XDG user-state database path; the DB is runtime state, never canonical.

    ``XDG_STATE_HOME`` is honoured when set, so a caller can point the registry
    at any writable state root (including a test sandbox) without a code change.
    """
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, APP_DIR, DB_NAME)


def connect(db: str | None = None, timeout: float = 10.0) -> sqlite3.Connection:
    """Open the registry, creating schema on first use.

    ``isolation_level=None`` keeps sqlite3 out of implicit transaction handling
    so the explicit ``BEGIN IMMEDIATE`` used by the writers is the only
    transaction boundary. This path CREATES and MIGRATES; a read that must not
    touch the database uses :func:`connect_readonly`.
    """
    path = db or default_db_path()
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = %d" % int(timeout * 1000))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.execute(
        "INSERT OR IGNORE INTO runs (run_id, created_at) VALUES ('', '')")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing registry up to this version's shape, additively.

    Only additions are made here, and only when the existing object predates
    them: a column is added when it is missing, never rewritten, and no row is
    mass-updated. The one migration so far is ``dispositions.verified_historical``
    (see :func:`_ensure_disposition_provenance`).
    """
    _ensure_disposition_provenance(conn)


def _ensure_disposition_provenance(conn: sqlite3.Connection) -> None:
    """Add the durable ``verified_historical`` proof bit if a DB predates it.

    ``ALTER TABLE ... ADD COLUMN`` is not idempotent, so it runs only when the
    column is absent (a fresh database already has it from ``SCHEMA``). Existing
    rows default to 0: a decision recorded before this column existed is
    caller-attributed and stays run-local, never silently promoted to a reusable
    historical clearance.
    """
    columns = {row["name"] for row in conn.execute(
        "PRAGMA table_info(dispositions)").fetchall()}
    if "verified_historical" not in columns:
        conn.execute("ALTER TABLE dispositions ADD COLUMN "
                     "verified_historical INTEGER NOT NULL DEFAULT 0")


def connect_readonly(db: str | None = None,
                     timeout: float = 10.0) -> sqlite3.Connection:
    """Open an EXISTING registry for reading, without creating or migrating.

    Opened through SQLite's ``mode=ro``, so a write is impossible by
    construction rather than by convention. An absent or unsupported database
    is an error the caller reports, not a database this path brings into
    existence. A read that repairs state it found is not a read.
    """
    path = db or default_db_path()
    if not os.path.isfile(path):
        raise RegistryError(
            "no registry database at %s; a read opens an existing registry and "
            "never creates one" % path)
    conn = sqlite3.connect("file:%s?mode=ro" % _sqlite_uri_path(path),
                           timeout=timeout, isolation_level=None, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = %d" % int(timeout * 1000))
    present = {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    missing = [table for table in REQUIRED_TABLES if table not in present]
    if missing:
        conn.close()
        raise RegistryError(
            "registry at %s is missing %s; it is not a registry this version "
            "reads. Migrate it with a writing command, or point --db elsewhere."
            % (path, ", ".join(missing)))
    return conn


def _sqlite_uri_path(path: str) -> str:
    """Absolute path escaped for a SQLite ``file:`` URI."""
    return quote(os.path.abspath(os.path.expanduser(path)))


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------

def normalize_paths(raw: list[str] | None) -> list[str]:
    """Repo-relative path prefixes, sorted and deduplicated."""
    out: set[str] = set()
    for item in raw or ():
        p = (item or "").strip().replace("\\", "/")
        if not p:
            raise RegistryError("empty path in scope")
        if "\n" in p:
            raise RegistryError("path contains a newline: %r" % item)
        if p.startswith("/"):
            raise RegistryError("path must be repo-relative: %r" % item)
        if re.match(r"^[A-Za-z]:", p):
            raise RegistryError("path must be repo-relative: %r" % item)
        parts = [seg for seg in p.split("/") if seg not in ("", ".")]
        if any(seg == ".." for seg in parts):
            raise RegistryError("path escapes the repository: %r" % item)
        if not parts:
            raise RegistryError("path resolves to the repository root: %r" % item)
        out.add("/".join(parts))
    return sorted(out)


def normalize_components(raw: list[str] | None) -> list[str]:
    """Component ids, case-folded and sorted for comparison."""
    out: set[str] = set()
    for item in raw or ():
        c = (item or "").strip().strip("/").strip()
        if not c:
            raise RegistryError("empty component in scope")
        if any(ch.isspace() for ch in c) or "\n" in c:
            raise RegistryError("invalid component: %r" % item)
        out.add(c.casefold())
    return sorted(out)


def normalize_mode(mode: str) -> str:
    m = (mode or "").strip().upper()
    if m not in MODES:
        raise RegistryError("mode must be one of %s: %r" % ("|".join(MODES), mode))
    return m


def _pack(seq: list[str]) -> str:
    return "\n".join(seq)


def _unpack(text: str | None) -> list[str]:
    return text.split("\n") if text else []


def _overlap(left: list[str], right: list[str]) -> list[str]:
    """First pair where one entry prefixes the other (components match exactly)."""
    for a in left:
        for b in right:
            if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                return [a, b]
    return []


# --------------------------------------------------------------------------
# Explicit runtime observation interface
# --------------------------------------------------------------------------

ACTIVE_STATES = ("active", "idle", "running", "working")
DEAD_STATES = ("done", "exited", "stopped", "failed", "killed", "dead",
               "finished")
UNKNOWN_LIVENESS = "UNKNOWN"


def agent_liveness(state: str) -> str:
    """Classify a worker state as ACTIVE, DEAD or UNKNOWN.

    Anything outside the known vocabulary is UNKNOWN rather than assumed
    healthy, because the difference decides whether a worktree counts as
    occupied. This vocabulary is generic; the core names no runtime.
    """
    value = (state or "").strip().lower()
    if value in ACTIVE_STATES:
        return "ACTIVE"
    if value in DEAD_STATES:
        return "DEAD"
    return "UNKNOWN"


@dataclass(frozen=True)
class AgentObservation:
    """One runtime occupant, as observed by the caller.

    Only identity, the filesystem location it occupies, and a state word are
    carried. No terminal content, title or private value crosses this boundary.
    """

    agent_id: str
    worktree: str
    state: str
    session_id: str = ""


@dataclass(frozen=True)
class RuntimeObservation:
    """One coherent occupancy observation, supplied by the caller.

    The core never fabricates one. ``available=False`` (or passing ``None``)
    means occupancy could not be read and a write cannot be vouched for. An
    ``available=True`` observation with no agents is a real answer: there is
    genuinely nobody occupying the probed worktrees.
    """

    available: bool
    agents: tuple[AgentObservation, ...] = ()
    reason: str = ""

    @classmethod
    def unavailable(cls, reason: str) -> "RuntimeObservation":
        return cls(False, (), reason)

    @classmethod
    def empty(cls) -> "RuntimeObservation":
        return cls(True, (), "")


def _coerce_observation(observation: RuntimeObservation | None) -> RuntimeObservation:
    """Validate a runtime observation into a canonical, safe value.

    A malformed observation — wrong type, non-bool ``available``, a non-empty
    ``reason`` on a healthy observation, a hostile/non-sequence ``agents``, or an
    agent missing/with mistyped fields — is converted into fixed UNAVAILABLE
    evidence. A valid observation is returned with its agents re-validated. No
    arbitrary caller ``reason`` is preserved, and the whole coercion is guarded
    so nothing it touches can raise out of it.
    """
    try:
        return _coerce_observation_inner(observation)
    except Exception:  # noqa: BLE001 - coercion never raises
        return RuntimeObservation.unavailable("observation unavailable")


def _coerce_observation_inner(observation) -> RuntimeObservation:
    unavailable = RuntimeObservation.unavailable("observation unavailable")
    # Exact built-in types only: an observation or agent subclass is rejected,
    # and every valid agent is rebuilt as a fresh plain AgentObservation so no
    # caller object is retained.
    if type(observation) is not RuntimeObservation:
        return unavailable
    if observation.available is not True:
        return unavailable
    if type(observation.reason) is not str or observation.reason != "":
        return unavailable
    agents = observation.agents
    if type(agents) is not tuple and type(agents) is not list:
        return unavailable
    validated: list[AgentObservation] = []
    for agent in agents:
        if type(agent) is not AgentObservation:
            return unavailable
        fields = (agent.agent_id, agent.worktree, agent.state,
                  agent.session_id)
        if any(type(value) is not str for value in fields):
            return unavailable
        if agent.worktree and not git.valid_abs_path(agent.worktree):
            return unavailable
        validated.append(AgentObservation(agent.agent_id, agent.worktree,
                                          agent.state, agent.session_id))
    return RuntimeObservation(True, tuple(validated), "")


# --------------------------------------------------------------------------
# Claim specification and evaluation
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ClaimSpec:
    run_id: str
    work_key: str
    owner: str
    mode: str
    issue: str = ""
    branch: str = ""
    worktree: str = ""

    def __post_init__(self) -> None:
        for field, value in (
            ("mode", normalize_mode(self.mode)),
            ("branch", (self.branch or "").strip()),
            ("worktree", git.normalize_worktree(self.worktree or "")),
        ):
            object.__setattr__(self, field, value)

    def validate(self) -> None:
        if not self.run_id.strip():
            raise RegistryError("empty run id")
        if not self.work_key.strip():
            raise RegistryError("empty work identity")
        if not self.owner.strip():
            raise RegistryError("empty owner")


def _refusal(status: str, spec: ClaimSpec, repo: str = "", **extra) -> dict:
    out = {
        "ok": False,
        "status": status,
        "resume": False,
        "run_id": spec.run_id,
        "work_key": spec.work_key,
        "issue": spec.issue,
        "owner": spec.owner,
        "mode": spec.mode,
        "repo": repo,
        "claim_id": None,
        "errors": [],
        "conflicts": [],
        "blocked_by": [],
        "resolved": [],
        "work": None,
    }
    out.update(extra)
    return out


def get_work(conn: sqlite3.Connection, run_id: str, work_key: str,
             issue: str = ""):
    return conn.execute(
        "SELECT * FROM works WHERE run_id=? AND work_key=? AND issue=?",
        (run_id, work_key, issue),
    ).fetchone()


def get_open_claim(conn: sqlite3.Connection, run_id: str, work_key: str,
                   issue: str, owner: str):
    return conn.execute(
        "SELECT * FROM claims WHERE run_id=? AND work_key=? AND issue=? "
        "AND owner=? AND released_at IS NULL",
        (run_id, work_key, issue, owner),
    ).fetchone()


def dependency_state(conn: sqlite3.Connection, run_id: str,
                     work_key: str) -> list[dict]:
    """Each declared dependency with whether a completed claim satisfies it."""
    rows = conn.execute(
        "SELECT depends_on FROM dependencies WHERE run_id=? AND work_key=? "
        "ORDER BY depends_on",
        (run_id, work_key),
    ).fetchall()
    out = []
    for row in rows:
        dep = row["depends_on"]
        done = conn.execute(
            "SELECT 1 FROM claims WHERE run_id=? AND work_key=? "
            "AND outcome='complete' LIMIT 1",
            (run_id, dep),
        ).fetchone()
        out.append({"depends_on": dep, "complete": done is not None})
    return out


def _conflict(spec: ClaimSpec, work, other) -> dict | None:
    """First reason ``spec`` may not run beside ``other``, or None when safe.

    ``other`` is a claim row joined to its work scope. Callers must already have
    restricted candidates to the same normalized repository.
    """
    if spec.mode == READ and other["mode"] == READ:
        return None
    if spec.mode == EXCLUSIVE or other["mode"] == EXCLUSIVE:
        return {
            "kind": "exclusive",
            "against_run": other["run_id"],
            "against_owner": other["owner"],
            "against_mode": other["mode"],
            "against_work": other["work_key"],
            "against_issue": other["issue"],
        }
    if spec.mode == READ or other["mode"] == READ:
        # A read never excludes a writer; only EXCLUSIVE excludes a read.
        return None
    # Both want to write.
    if spec.work_key == other["work_key"] and spec.issue == other["issue"]:
        return {
            "kind": "work_identity",
            "against_run": other["run_id"],
            "against_owner": other["owner"],
            "against_mode": other["mode"],
            "against_work": other["work_key"],
            "against_issue": other["issue"],
            "detail": "an independent writer may not join work already held",
        }
    # One accountable owner may hold several writer claims in one worktree on
    # one branch: that is governance claiming beside task work, not two writers.
    same_owner_checkout = (
        spec.run_id == other["run_id"]
        and spec.owner == other["owner"]
        and bool(spec.worktree)
        and spec.worktree == other["worktree"]
    )
    if not same_owner_checkout:
        if spec.branch and spec.branch == other["branch"]:
            return {
                "kind": "branch",
                "value": spec.branch,
                "against_run": other["run_id"],
                "against_owner": other["owner"],
                "against_work": other["work_key"],
            }
        if spec.worktree and spec.worktree == other["worktree"]:
            return {
                "kind": "worktree",
                "value": spec.worktree,
                "against_run": other["run_id"],
                "against_owner": other["owner"],
                "against_work": other["work_key"],
            }
    hit = _overlap(_unpack(work["paths"]), _unpack(other["paths"]))
    if hit:
        return {
            "kind": "path",
            "paths": hit,
            "against_run": other["run_id"],
            "against_owner": other["owner"],
            "against_work": other["work_key"],
        }
    hit = _overlap(_unpack(work["components"]), _unpack(other["components"]))
    if hit:
        return {
            "kind": "component",
            "components": hit,
            "against_run": other["run_id"],
            "against_owner": other["owner"],
            "against_work": other["work_key"],
        }
    return None


def _same_issue_join(other) -> dict:
    """A nonempty issue already held anywhere in this repository."""
    return {
        "kind": "issue_join",
        "issue": other["issue"],
        "against_run": other["run_id"],
        "against_work": other["work_key"],
        "against_owner": other["owner"],
        "against_mode": other["mode"],
        "claim_id": other["claim_id"],
    }


def evaluate(conn: sqlite3.Connection, spec: ClaimSpec) -> dict:
    """Decide ``spec`` against every open claim in its repository.

    Deliberately not scoped to one run or one owner: a writer in another run or
    another session still holds the repository, and a same-owner claim on
    different work still occupies the scope.
    """
    work = get_work(conn, spec.run_id, spec.work_key, spec.issue)
    if work is None:
        return _refusal(
            ERROR, spec, errors=["work %r is not proposed in run %r"
                                 % (spec.work_key, spec.run_id)])

    echo = {"paths": _unpack(work["paths"]),
            "components": _unpack(work["components"]),
            "repo": work["repo"]}

    existing = get_open_claim(conn, spec.run_id, spec.work_key, spec.issue,
                              spec.owner)
    if existing is not None:
        drift = []
        if existing["mode"] != spec.mode:
            drift.append("mode %s->%s" % (existing["mode"], spec.mode))
        if existing["branch"] != spec.branch:
            drift.append("branch %r->%r" % (existing["branch"], spec.branch))
        if existing["worktree"] != spec.worktree:
            drift.append("worktree %r->%r" % (existing["worktree"], spec.worktree))
        if drift:
            return _refusal(
                ERROR, spec, repo=work["repo"], work=echo,
                claim_id=existing["claim_id"],
                errors=["resume mismatch on claim %d: %s"
                        % (existing["claim_id"], ", ".join(drift))])
        out = _refusal(ERROR, spec, repo=work["repo"], work=echo)
        out.update({"ok": True, "status": RESUMED, "resume": True,
                    "claim_id": existing["claim_id"],
                    "claimed_at": existing["claimed_at"]})
        return out

    others = conn.execute(
        "SELECT c.*, w.repo AS repo, w.paths AS paths, "
        "w.components AS components FROM claims c JOIN works w "
        "  ON w.run_id=c.run_id AND w.work_key=c.work_key AND w.issue=c.issue "
        "WHERE c.released_at IS NULL AND w.repo=?",
        (work["repo"],),
    ).fetchall()

    if spec.issue:
        held = [o for o in others if o["issue"] == spec.issue]
        if held:
            out = _refusal(JOIN_EXISTING, spec, repo=work["repo"], work=echo)
            out["conflicts"] = [_same_issue_join(o) for o in held]
            return out

    deps = dependency_state(conn, spec.run_id, spec.work_key)
    pending = [d["depends_on"] for d in deps if not d["complete"]]
    if pending:
        out = _refusal(BLOCKED, spec, repo=work["repo"], work=echo,
                       dependencies=deps)
        out["blocked_by"] = pending
        return out

    conflicts = []
    for other in others:
        reason = _conflict(spec, work, other)
        if reason is not None:
            conflicts.append(reason)
    if conflicts:
        out = _refusal(CONFLICT, spec, repo=work["repo"], work=echo)
        out["conflicts"] = conflicts
        return out

    out = _refusal(ERROR, spec, repo=work["repo"], work=echo)
    out.update({"ok": True, "status": CLAIMED})
    return out


# --------------------------------------------------------------------------
# Proposal
# --------------------------------------------------------------------------

def propose(conn: sqlite3.Connection, run_id: str, work_key: str, repo: str,
            paths: list[str] | None = None,
            components: list[str] | None = None, issue: str = "",
            depends_on: list[str] | None = None,
            objective: str = "") -> dict:
    """Register a work item with its normalized scope.

    Re-proposing identical scope is a no-op. Changed scope is accepted only
    while no claim is open, so a claim's scope cannot move underneath it.

    ``objective`` is optional free text used only by the advisory overlap scan.
    It is not scope and never grants anything.
    """
    if not run_id.strip():
        raise RegistryError("empty run id")
    if not work_key.strip():
        raise RegistryError("empty work identity")
    norm_repo = git.normalize_repo(repo)
    norm_paths = normalize_paths(paths)
    norm_components = normalize_components(components)
    if not norm_paths and not norm_components:
        raise RegistryError("empty scope: declare at least one path or component")

    deps = sorted({(d or "").strip() for d in (depends_on or ())
                   if (d or "").strip()})
    if work_key in deps:
        raise RegistryError("work %r depends on itself" % work_key)
    objective = (objective or "").strip()

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO runs (run_id, created_at) VALUES (?,?)",
            (run_id, now()))
        existing = get_work(conn, run_id, work_key, issue)
        if existing is None:
            conn.execute(
                "INSERT INTO works (run_id, work_key, issue, repo, paths, "
                "components, objective, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (run_id, work_key, issue, norm_repo, _pack(norm_paths),
                 _pack(norm_components), objective, now()))
            status = PROPOSED
        else:
            unchanged = (
                existing["repo"] == norm_repo
                and existing["paths"] == _pack(norm_paths)
                and existing["components"] == _pack(norm_components)
                and (existing["objective"] or "") == objective)
            if unchanged:
                status = UNCHANGED
            else:
                open_rows = conn.execute(
                    "SELECT 1 FROM claims WHERE run_id=? AND work_key=? "
                    "AND issue=? AND released_at IS NULL LIMIT 1",
                    (run_id, work_key, issue)).fetchall()
                if open_rows:
                    conn.execute("ROLLBACK")
                    return {"ok": False, "status": ERROR,
                            "errors": ["scope of %r is fixed while a claim is "
                                       "open" % work_key]}
                conn.execute(
                    "UPDATE works SET repo=?, paths=?, components=?, objective=? "
                    "WHERE run_id=? AND work_key=? AND issue=?",
                    (norm_repo, _pack(norm_paths), _pack(norm_components),
                     objective, run_id, work_key, issue))
                status = AMENDED
        if deps:
            conn.execute(
                "DELETE FROM dependencies WHERE run_id=? AND work_key=?",
                (run_id, work_key))
            conn.executemany(
                "INSERT OR IGNORE INTO dependencies (run_id, work_key, "
                "depends_on) VALUES (?,?,?)",
                [(run_id, work_key, dep) for dep in deps])
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise

    return {"ok": True, "status": status, "run_id": run_id,
            "work_key": work_key, "issue": issue, "repo": norm_repo,
            "paths": norm_paths, "components": norm_components,
            "objective": objective, "depends_on": deps}


# --------------------------------------------------------------------------
# Live evidence: Git probes plus an explicit runtime observation
# --------------------------------------------------------------------------

def gather_evidence(repo: str, observation: RuntimeObservation | None,
                    worktree: str = "") -> dict:
    """Gather the live sources for ``repo``, guarded at the outer boundary.

    Git is probed here (read-only). Occupancy comes only from the supplied
    :class:`RuntimeObservation`; the core never invents one. The result is the
    sanitized canonical evidence envelope: an invalid probe or observation makes
    ``ok`` False, and no caller-supplied field is copied through. Any unexpected
    failure inside the boundary returns fixed UNAVAILABLE evidence.

    Probes run outside any SQLite transaction, so a slow probe never holds the
    registry's write lock.
    """
    try:
        return _gather_evidence_inner(repo, observation, worktree)
    except Exception:  # noqa: BLE001 - gather_evidence never raises
        return _unavailable_evidence()


def _gather_evidence_inner(repo, observation, worktree) -> dict:
    identity = ""
    try:
        identity = git.normalize_repo(repo)
    except ValueError:
        identity = ""
    return _canonical_evidence({
        "ok": True,
        "repo": os.path.realpath(os.path.expanduser(repo)),
        "identity": identity,
        "worktree": worktree,
        "worktrees": git.probe_worktrees(repo),
        "baseline_probe": git.probe_baseline_ref(repo),
        "observation": observation,
        "errors": [],
    })


# Fixed diagnostics for the allowlisted probe reason codes. No arbitrary probe
# field is ever interpolated into a public string.
_DIAGNOSTICS = {
    "probe_failed": "probe failed",
    "probe_malformed": "probe malformed",
    "": "unavailable",
}

_WORKTREE_FIELDS = frozenset({"path", "head", "branch", "detached", "bare",
                               "prunable", "locked"})
_STATUS_FIELDS = frozenset({"index", "worktree", "path", "orig_path", "staged",
                            "unstaged", "untracked"})
_PROBE_SOURCES = frozenset({"git"})

# PER-PROBE allowed envelope keys. ``items`` is only allowed where the schema
# actually uses it, so a dirty-digest probe carrying ``items`` is rejected.
_PROBE_ALLOWED_KEYS = {
    "worktrees": frozenset({"ok", "source", "reason", "error", "items"}),
    "baseline": frozenset({"ok", "source", "reason", "error", "items"}),
    "status": frozenset({"ok", "source", "reason", "error", "items",
                         "worktree"}),
    "committed": frozenset({"ok", "source", "reason", "error", "items",
                            "worktree", "base"}),
    "dirty_digest": frozenset({"ok", "source", "reason", "error", "worktree",
                               "clean", "digest"}),
}


def _healthy_envelope(probe, kind: str) -> bool:
    """Require a healthy probe envelope with EXACTLY the allowed keys.

    ``type(probe) is dict`` rejects subclasses, the key set must equal the
    per-probe allowed set (missing or extra keys fail), ``ok`` must be literally
    True, ``source`` must be allowlisted, and ``reason``/``error`` must be exact
    empty strings.
    """
    if type(probe) is not dict:
        return False
    if set(probe) != _PROBE_ALLOWED_KEYS[kind]:
        return False
    if probe["ok"] is not True:
        return False
    if probe["source"] not in _PROBE_SOURCES:
        return False
    reason, error = probe["reason"], probe["error"]
    if type(reason) is not str or type(error) is not str:
        return False
    return reason == "" and error == ""


def _valid_worktree_path(path) -> bool:
    """An absolute, canonical, NUL-free, existing directory path."""
    if not git.valid_abs_path(path):
        return False
    try:
        return os.path.isdir(path)
    except Exception:  # noqa: BLE001 - a hostile path argument
        return False


def _canonical_worktree_item(item):
    """A canonical plain copy of a worktree item, or ``None``."""
    if type(item) is not dict or set(item) != _WORKTREE_FIELDS:
        return None
    path, head, branch = item["path"], item["head"], item["branch"]
    detached, bare = item["detached"], item["bare"]
    prunable, locked = item["prunable"], item["locked"]
    if not _valid_worktree_path(path):
        return None
    if not git.valid_commit_id(head):
        return None
    if type(branch) is not str:
        return None
    if branch != "" and not git.valid_short_ref(branch):
        return None
    # The ONE shared worktree-state helper the probe also uses.
    if not git.valid_worktree_state(branch, detached, bare):
        return None
    if type(prunable) is not bool or type(locked) is not bool:
        return None
    return {"path": path, "head": head, "branch": branch,
            "detached": detached, "bare": bare,
            "prunable": prunable, "locked": locked}


def _canonical_status_item(item):
    """A canonical plain copy of a status item, or ``None``."""
    if type(item) is not dict or set(item) != _STATUS_FIELDS:
        return None
    index, worktree = item["index"], item["worktree"]
    path, orig = item["path"], item["orig_path"]
    staged, unstaged = item["staged"], item["unstaged"]
    untracked = item["untracked"]
    if type(index) is not str or type(worktree) is not str:
        return None
    if not git.valid_status_pair(index, worktree):
        return None
    if not git.valid_relpath(path):
        return None
    if type(orig) is not str:
        return None
    rename = index in ("R", "C") or worktree in ("R", "C")
    if rename:
        if not orig or not git.valid_relpath(orig) or orig == path:
            return None
    elif orig != "":
        return None
    if not all(type(flag) is bool for flag in (staged, unstaged, untracked)):
        return None
    # The ONE shared flag derivation; supplied flags must match it exactly.
    if (staged, unstaged, untracked) != git.status_flags(index, worktree):
        return None
    return {"index": index, "worktree": worktree, "path": path,
            "orig_path": orig, "staged": staged, "unstaged": unstaged,
            "untracked": untracked}


def _canonical_worktrees_probe(probe):
    if not _healthy_envelope(probe, "worktrees"):
        return None
    items = probe["items"]
    if type(items) is not list or not items:
        return None
    out = []
    for item in items:
        canonical = _canonical_worktree_item(item)
        if canonical is None:
            return None
        out.append(canonical)
    if len({item["path"] for item in out}) != len(out):
        return None
    return {"ok": True, "source": "git", "reason": "", "error": "",
            "items": out}


def _canonical_status_probe(probe):
    if not _healthy_envelope(probe, "status"):
        return None
    worktree = probe["worktree"]
    if not _valid_worktree_path(worktree):
        return None
    items = probe["items"]
    if type(items) is not list:
        return None
    out = []
    for item in items:
        canonical = _canonical_status_item(item)
        if canonical is None:
            return None
        out.append(canonical)
    keys = {(i["index"], i["worktree"], i["path"], i["orig_path"]) for i in out}
    if len(keys) != len(out):
        return None
    return {"ok": True, "source": "git", "reason": "", "error": "",
            "worktree": worktree, "items": out}


def _canonical_committed_probe(probe):
    if not _healthy_envelope(probe, "committed"):
        return None
    worktree, base = probe["worktree"], probe["base"]
    if not _valid_worktree_path(worktree):
        return None
    if not git.valid_commit_id(base):
        return None
    items = probe["items"]
    if type(items) is not list:
        return None
    out = []
    for path in items:
        if not git.valid_relpath(path):
            return None
        out.append(path)
    if len(set(out)) != len(out):
        return None
    return {"ok": True, "source": "git", "reason": "", "error": "",
            "worktree": worktree, "base": base, "items": out}


def _canonical_baseline_probe(probe):
    if not _healthy_envelope(probe, "baseline"):
        return None
    items = probe["items"]
    if type(items) is not list or len(items) > 1:
        return None
    out = []
    for ref in items:
        if not git.valid_short_ref(ref):
            return None
        out.append(ref)
    return {"ok": True, "source": "git", "reason": "", "error": "",
            "items": out}


def _canonical_dirty_digest(probe):
    if not _healthy_envelope(probe, "dirty_digest"):
        return None
    worktree, clean, digest = probe["worktree"], probe["clean"], probe["digest"]
    if not _valid_worktree_path(worktree):
        return None
    if type(clean) is not bool or type(digest) is not str:
        return None
    if clean:
        if digest != "":
            return None
    elif not git.valid_digest(digest):
        return None
    return {"ok": True, "source": "git", "reason": "", "error": "",
            "worktree": worktree, "clean": clean, "digest": digest}


_PROBE_VALIDATORS = {
    "worktrees": _canonical_worktrees_probe,
    "status": _canonical_status_probe,
    "committed": _canonical_committed_probe,
    "baseline": _canonical_baseline_probe,
    "dirty_digest": _canonical_dirty_digest,
}


def _canonical_probe(probe, kind: str):
    """The ONE schema decision for a probe result.

    Returns a canonical PLAIN COPY only when the probe's ``ok`` flag, exact
    per-probe key set, git semantics and item contents are all valid; otherwise
    returns ``None`` (canonical failure). Consumers read only the copy, never
    the caller's object, so a hostile or mutable subclass cannot flip a value
    after validation. The whole decision is guarded, so it never raises.
    """
    try:
        validator = _PROBE_VALIDATORS.get(kind)
        if validator is None:
            return None
        return validator(probe)
    except Exception:  # noqa: BLE001 - a validator never raises
        return None


def _probe_items(probe, kind: str):
    """Validated ``items`` for a probe, or ``None`` on any mismatch."""
    canonical = _canonical_probe(probe, kind)
    if canonical is None:
        return None
    return canonical["items"]


def _diagnostic(probe) -> str:
    """A fixed diagnostic from an allowlisted reason code only."""
    if not isinstance(probe, dict):
        return _DIAGNOSTICS[""]
    reason = probe.get("reason")
    if isinstance(reason, str) and reason in _DIAGNOSTICS:
        return _DIAGNOSTICS[reason]
    return _DIAGNOSTICS[""]


def _probe_failure(what: str) -> dict:
    """A canonical failure probe with a fixed, non-echoing diagnostic."""
    return {"ok": False, "source": "git", "reason": "probe_failed",
            "error": "%s could not be read" % what, "items": None}


_EVIDENCE_KEYS = frozenset({"ok", "errors", "repo", "identity", "worktree",
                            "worktrees", "baseline_probe", "observation"})
# The canonical envelope adds these two; canonicalization must be idempotent so
# a canonical envelope can be re-validated (e.g. write_gate -> reconcile).
_EVIDENCE_ALLOWED_KEYS = _EVIDENCE_KEYS | frozenset({"baseline",
                                                     "baseline_warnings"})


def _safe_abs_or_empty(value) -> str:
    """A trusted existing absolute path, or "" when the value is not one.

    Rebuilds identities from a validated filesystem fact rather than copying an
    arbitrary caller string, so a synthetic value cannot survive as a public
    repository/identity/worktree.
    """
    if not git.valid_abs_path(value):
        return ""
    try:
        return value if os.path.isdir(value) else ""
    except Exception:  # noqa: BLE001 - a hostile path argument
        return ""


def _canonical_evidence(evidence) -> dict:
    """The ONE aggregate evidence-envelope decision.

    Returns a sanitized canonical envelope. Evidence is good only when every
    mandatory key is present, its declared ``ok`` is literally True, its
    ``errors`` field is an empty list (ok and errors must agree), both required
    probes pass the per-probe schema decision, and the runtime observation is
    valid. Caller-supplied warnings and identities are never copied: paths are
    validated and the warnings are rebuilt. The whole decision is guarded, so a
    hostile container cannot make it raise.
    """
    try:
        return _canonical_evidence_inner(evidence)
    except Exception:  # noqa: BLE001 - a validator never raises
        return _unavailable_evidence()


def _unavailable_evidence() -> dict:
    return {
        "ok": False,
        "repo": "",
        "identity": "",
        "worktree": "",
        "worktrees": _probe_failure("git worktree"),
        "baseline": "",
        "baseline_probe": _probe_failure("git baseline"),
        "baseline_warnings": [],
        "observation": RuntimeObservation.unavailable("observation unavailable"),
        "errors": ["live evidence is unavailable"],
    }


def _canonical_evidence_inner(evidence) -> dict:
    if type(evidence) is not dict:
        return _unavailable_evidence()
    if not _EVIDENCE_KEYS <= set(evidence) \
            or set(evidence) - _EVIDENCE_ALLOWED_KEYS:
        return _unavailable_evidence()
    obs = _coerce_observation(evidence["observation"])
    worktrees = _canonical_probe(evidence["worktrees"], "worktrees")
    baseline_probe = _canonical_probe(evidence["baseline_probe"], "baseline")
    raw_errors = evidence["errors"]
    errors_consistent = type(raw_errors) is list and len(raw_errors) == 0
    declared_ok = evidence["ok"] is True
    # Required identities must be REAL, existing paths. An invalid one makes the
    # whole envelope unavailable rather than being silently blanked to "".
    repo = _safe_abs_or_empty(evidence["repo"])
    identity = _safe_abs_or_empty(evidence["identity"])
    raw_worktree = evidence["worktree"]
    if type(raw_worktree) is not str:
        worktree, worktree_valid = "", False
    elif raw_worktree == "":
        worktree, worktree_valid = "", True
    else:
        worktree = _safe_abs_or_empty(raw_worktree)
        worktree_valid = bool(worktree)
    identity_ok = bool(repo) and bool(identity)
    # The repository path must RESOLVE to the claimed identity: an existing
    # directory that is not the repository the writer claims is not evidence.
    identity_matches = False
    if identity_ok:
        try:
            identity_matches = git.normalize_repo(repo) == identity
        except Exception:  # noqa: BLE001 - a hostile path argument
            identity_matches = False
    ok = (declared_ok and errors_consistent and worktrees is not None
          and baseline_probe is not None and obs.available and identity_ok
          and identity_matches and worktree_valid)
    errors: list[str] = []
    if not ok:
        if worktrees is None:
            errors.append("git worktree: %s"
                          % _diagnostic(evidence["worktrees"]))
        if baseline_probe is None:
            errors.append("git baseline: %s"
                          % _diagnostic(evidence["baseline_probe"]))
        if not obs.available:
            errors.append("runtime: observation unavailable")
        if not declared_ok:
            errors.append("live evidence is unavailable")
        if not errors_consistent:
            errors.append("live evidence is inconsistent")
        if not identity_ok:
            errors.append("live evidence lacks a verified repository identity")
        elif not identity_matches:
            errors.append("live evidence repository does not match its "
                          "identity")
        if not worktree_valid:
            errors.append("live evidence lacks a verified worktree binding")
        if not errors:
            errors.append("live evidence is unavailable")
    baseline_items = (_probe_items(baseline_probe, "baseline")
                      if baseline_probe is not None else None)
    baseline = baseline_items[0] if baseline_items else ""
    return {
        "ok": ok,
        "repo": repo,
        "identity": identity,
        "worktree": worktree,
        "worktrees": (worktrees if worktrees is not None
                      else _probe_failure("git worktree")),
        "baseline": baseline,
        "baseline_probe": (baseline_probe if baseline_probe is not None
                           else _probe_failure("git baseline")),
        # Rebuilt, never copied from the caller.
        "baseline_warnings": git.baseline_warnings(repo, baseline),
        "observation": obs,
        "errors": errors,
    }


def _nearest_worktree(cwd: str, worktrees: list[dict]) -> str:
    """The worktree containing ``cwd``, tolerating a nested subdirectory."""
    if not cwd:
        return ""
    paths = sorted({tree["path"] for tree in worktrees}, key=len, reverse=True)
    for path in paths:
        if cwd == path or cwd.startswith(path + os.sep):
            return path
    return ""


def _agent_identity(record: dict) -> dict:
    """Material live identity for one agent: identity only, never content."""
    return {
        "agent_id": record.get("agent_id", ""),
        "worktree": record.get("worktree", ""),
        "state": record.get("state", ""),
        "session_id": record.get("session_id", ""),
        "liveness": record.get("liveness", ""),
    }


def _live_in_repo(agents: list[AgentObservation],
                  worktrees: list[dict]) -> dict[str, list[dict]]:
    """Index agents by the worktree they occupy, with explicit liveness.

    An agent in an unrelated repository must never make this repository look
    occupied, so entries are filtered to this repository's worktrees. A nested
    location maps to its nearest worktree. DEAD and UNKNOWN records are kept
    rather than dropped: a dead worker over dirty work is exactly the stale case
    that must stay visible.
    """
    by_path: dict[str, list[dict]] = {}
    for agent in agents:
        path = _nearest_worktree(agent.worktree, worktrees)
        if not path:
            continue
        record = {
            "agent_id": agent.agent_id,
            "worktree": os.path.realpath(agent.worktree) if agent.worktree else "",
            "state": agent.state,
            "session_id": agent.session_id,
            "liveness": agent_liveness(agent.state),
        }
        by_path.setdefault(path, []).append(record)
    return by_path


def _paths_within(path: str, prefixes: list[str]) -> bool:
    """True when ``path`` lies inside any declared scope prefix."""
    for prefix in prefixes:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def _paths_overlap(paths: list[str], prefixes: list[str]) -> list[str]:
    """Paths inside scope, or scope prefixes touched by a path."""
    hits = set()
    for path in paths or ():
        for prefix in prefixes or ():
            if path == prefix or path.startswith(prefix + "/") \
                    or prefix.startswith(path + "/"):
                hits.add(path)
    return sorted(hits)


def spec_is_writer(entry: dict) -> bool:
    """Whether a finding describes a claim that intends to write."""
    return (entry.get("mode") or "").strip().upper() in (WRITE, EXCLUSIVE)


def _classify_claim(entry: dict, live: bool) -> None:
    """Turn measured evidence into a finding kind.

    Precedence is UNKNOWN over SCOPE_DRIFT over STALE_DIRTY over LIVE, and there
    is exactly one copy of it so containment and classification cannot drift.
    """
    if entry.get("scope_unknown") or entry.get("committed_unknown") \
            or entry.get("dirty_digest_unknown") \
            or entry.get("worker_state_unknown") \
            or entry.get("status_unknown") \
            or entry.get("worktree_absent"):
        entry["kind"] = UNKNOWN
        entry["status"] = "scope_unknown"
    elif entry.get("escaped"):
        entry["kind"] = SCOPE_DRIFT
        entry["status"] = "scope_drift"
    elif (entry.get("changes") or entry.get("committed")) and not live:
        entry["kind"] = STALE_DIRTY
        entry["status"] = "stale_dirty"
    elif entry.get("changes") or entry.get("committed"):
        entry["kind"] = LIVE
        entry["status"] = "live_dirty"
    elif not live:
        entry["kind"] = STALE_CLEAN
        entry["status"] = "stale_clean"
    else:
        entry["kind"] = LIVE
        entry["status"] = "live_clean"


def released_source_provenance(conn: sqlite3.Connection,
                               finding: dict) -> dict | None:
    """Ledger-verified provenance of a preserved-unmerged released worktree.

    Returns a compact record ONLY when the Registry's own durable ledger proves
    the finding is the clean leftover of a completed, released claim. All of:

    * the finding is unregistered work with no live occupant, no uncommitted
      change and no unreadable evidence (a live, dirty, detached or unknown
      worktree is a blocker, never "historical");
    * its exact HEAD equals a released claim's ``released_head`` for the SAME
      repository identity and the SAME physical checkout;
    * the released claim completed, declared a branch the checkout still has
      attached, and wrote in WRITE/EXCLUSIVE mode;
    * every committed path lies inside that released claim's declared scope.

    The answer is derived from the ledger and the live-derived finding alone:
    the repository identity is re-derived from the finding's worktree, so a
    caller cannot point one repository's finding at another repository's
    release. No run id, authority string or self-attributed provenance is ever
    the basis for the answer. Anything unreadable, ambiguous or unmatched
    returns ``None`` (fail closed), so an active or unknown writer can never be
    misread as historical.
    """
    try:
        return _released_source_provenance_inner(conn, finding)
    except Exception:  # noqa: BLE001 - a verification never raises
        return None


def _released_source_provenance_inner(conn, finding) -> dict | None:
    if type(finding) is not dict:
        return None
    if finding.get("kind") != DISCOVERED_UNREGISTERED \
            or finding.get("claim_id") is not None:
        return None
    if finding.get("unverifiable") or finding.get("live"):
        return None
    for flag in ("worktree_absent", "scope_unknown", "committed_unknown",
                 "dirty_digest_unknown", "worker_state_unknown"):
        if finding.get(flag):
            return None
    if finding.get("changes") or finding.get("status_codes") \
            or finding.get("detached"):
        return None
    head = finding.get("head")
    if not git.valid_commit_id(head):
        return None
    committed = finding.get("committed")
    if type(committed) is not list or not committed \
            or not all(git.valid_relpath(path) for path in committed):
        return None
    worktree = finding.get("worktree")
    if not git.valid_abs_path(worktree):
        return None
    real = os.path.realpath(worktree)
    identity = git.normalize_repo(real)
    if not identity:
        return None
    rows = conn.execute(
        "SELECT c.claim_id, c.run_id, c.work_key, c.issue, c.owner, c.mode, "
        "c.branch, c.worktree, c.released_at, c.outcome, c.released_head, "
        "w.repo AS repo, w.paths AS paths "
        "FROM claims c JOIN works w "
        "  ON w.run_id=c.run_id AND w.work_key=c.work_key AND w.issue=c.issue "
        "WHERE c.released_at IS NOT NULL AND c.outcome='complete' "
        "  AND c.released_head=? AND c.mode IN (?,?) AND w.repo=?",
        (head, WRITE, EXCLUSIVE, identity),
    ).fetchall()
    branch = finding.get("branch")
    matches = []
    for row in rows:
        if not row["owner"] or not row["branch"] or row["branch"] != branch:
            continue
        if os.path.realpath(row["worktree"]) != real:
            continue
        scope = _unpack(row["paths"])
        if not scope or not all(_paths_within(path, scope) for path in committed):
            continue
        matches.append(row)
    if len(matches) != 1:
        # Zero matches is not provenance; more than one is an ambiguity the
        # ledger cannot resolve, so it stays a blocker rather than being guessed.
        return None
    row = matches[0]
    return {
        "claim_id": row["claim_id"],
        "run_id": row["run_id"],
        "work_key": row["work_key"],
        "issue": row["issue"],
        "owner": row["owner"],
        "mode": row["mode"],
        "branch": row["branch"],
        "released_head": row["released_head"],
        "released_at": row["released_at"],
        "repo": row["repo"],
        "scope": _unpack(row["paths"]),
    }


def reconcile(conn: sqlite3.Connection, evidence: dict) -> dict:
    """Compare this repository's open claims against live evidence.

    Guards the whole decision: any unexpected failure from a hostile evidence
    value returns fail-closed evidence instead of propagating.
    """
    try:
        return _reconcile_inner(conn, evidence)
    except Exception:  # noqa: BLE001 - reconcile never raises
        return {
            "ok": False, "status": "unknown", "repo": "", "identity": "",
            "baseline": "", "baseline_warnings": [], "occupancy_unknown": True,
            "findings": [], "probable_overlaps": [],
            "errors": ["live evidence is unavailable"],
        }


def _reconcile_inner(conn: sqlite3.Connection, evidence: dict) -> dict:
    """Compare this repository's open claims against live evidence.

    Reads claims and returns findings; never writes. The caller persists.

    Findings:

    * ``DISCOVERED_UNREGISTERED`` - live or dirty work with no registry entry
    * ``SCOPE_DRIFT``             - a claimed worktree changed outside its scope
    * ``STALE_DIRTY``             - a claim whose agent is gone, changes remain
    * ``STALE_CLEAN``             - a claim whose agent is gone, nothing dirty
    * ``UNKNOWN``                 - live state could not be read for a worktree

    Repository scoping is by normalized Git common-dir identity, so claims in
    other repositories are excluded rather than reported as absent worktrees.
    """
    canonical = _canonical_evidence(evidence)
    repo = canonical["repo"]
    identity = canonical["identity"]
    obs = canonical["observation"]
    occupancy_unknown = not obs.available
    if not canonical["ok"]:
        # Unavailable or malformed evidence is NOT an empty reading.
        return {
            "ok": False,
            "status": "unknown",
            "repo": repo,
            "identity": identity,
            "baseline": "",
            "baseline_warnings": [],
            "occupancy_unknown": occupancy_unknown,
            "findings": [],
            "probable_overlaps": [],
            "errors": list(canonical["errors"]),
        }
    worktree_probe = canonical["worktrees"]
    worktrees = _probe_items(worktree_probe, "worktrees")
    baseline = canonical["baseline"]
    agents = list(obs.agents) if obs.available else []
    worktree_by_path = {tree["path"]: tree for tree in worktrees}
    live_by_path = _live_in_repo(agents, worktrees) if agents else {}
    findings: list[dict] = []

    claims = [c for c in list_active_claims(conn)
              if not identity
              or git.normalize_repo(c.get("repo") or repo) == identity]
    claimed_worktrees: set[str] = set()

    for claim in claims:
        worktree = claim.get("worktree") or ""
        scope = claim.get("paths") or []
        components = claim.get("components") or []
        entry: dict = {
            "kind": LIVE,
            "claim_id": claim["claim_id"],
            "run_id": claim["run_id"],
            "work_key": claim["work_key"],
            "issue": claim["issue"],
            "owner": claim["owner"],
            "mode": claim["mode"],
            "branch": claim.get("branch", ""),
            "worktree": worktree,
            "scope": list(scope),
            "components": list(components),
            "status": "",
            "changes": [],
            "committed": [],
            "escaped": [],
            "scope_known": bool(scope),
        }
        findings.append(entry)

        if worktree and spec_is_writer(entry):
            claimed_worktrees.add(os.path.realpath(worktree))

        # A writer missing either binding cannot be placed or vouched for. This
        # is decided BEFORE any verified checkout classification, so an unbound
        # writer can never read as STALE_CLEAN or LIVE, with or without an
        # occupant, in either writer mode.
        if spec_is_writer(entry) and (not worktree or not claim.get("branch")):
            entry["status"] = "unbound_writer"
            entry["kind"] = UNKNOWN
            entry["unverifiable"] = True
            entry["scope_unknown"] = True
            continue

        if not worktree:
            # A READ claim declares nothing about the filesystem and stays
            # informational rather than unverifiable.
            entry["status"] = "no_worktree"
            continue

        if worktree not in worktree_by_path:
            entry["status"] = "worktree_absent"
            entry["unverifiable"] = True
            entry["worktree_absent"] = True
            _classify_claim(entry, False)
            continue

        tree = worktree_by_path[worktree]
        live_here = [a for a in live_by_path.get(worktree, [])
                     if a["liveness"] == "ACTIVE"]
        others = [a for a in live_by_path.get(worktree, [])
                  if a["liveness"] != "ACTIVE"]
        entry["live"] = bool(live_here)
        entry["workers"] = [_agent_identity(a) for a in live_here + others]
        entry["worker_state_unknown"] = (
            any(a.get("liveness") == UNKNOWN_LIVENESS for a in others)
            or occupancy_unknown)
        if entry["worker_state_unknown"]:
            entry["unverifiable"] = True
        entry["head"] = tree.get("head", "")
        entry["worktree_branch"] = tree.get("branch", "")
        entry["detached"] = tree.get("detached", False)
        # Branch drift is measured once, for every consumer. A detached HEAD
        # counts as drift too: a claim that declared an attached branch no
        # longer describes a checkout that has none.
        if claim.get("branch") and claim["branch"] != tree.get("branch", ""):
            entry["branch_drift"] = "%s -> %s" % (
                claim["branch"], tree.get("branch") or "(detached)")

        status = git.probe_status(worktree)
        status_items = _probe_items(status, "status")
        if status_items is None:
            entry["status"] = "unknown"
            entry["status_unknown"] = True
            entry["unverifiable"] = True
            entry["detail"] = _diagnostic(status)
            _classify_claim(entry, bool(live_here))
            continue
        entry["changes"] = git.touched_paths(status_items)
        entry["status_codes"] = sorted({
            "%s%s" % (c.get("index", "?"), c.get("worktree", "?"))
            for c in status_items})
        digest = git.probe_dirty_digest(worktree)
        canonical_digest = _canonical_probe(digest, "dirty_digest")
        if canonical_digest is not None:
            # Read ONLY the canonical copy, never the caller's object.
            entry["dirty_digest"] = canonical_digest["digest"]
        else:
            entry["dirty_digest_unknown"] = True
            entry["unverifiable"] = True
            entry["detail"] = _diagnostic(digest)

        if baseline:
            committed_items = _probe_items(
                git.probe_committed_diff(worktree, baseline), "committed")
            if committed_items is not None:
                entry["committed"] = committed_items
            else:
                entry["committed_unknown"] = True
                entry["unverifiable"] = True
        else:
            entry["committed_unknown"] = True
            entry["unverifiable"] = True
            entry["baseline_absent"] = True

        considered = set(entry["changes"]) | set(entry.get("committed") or [])
        if scope:
            entry["escaped"] = sorted(
                p for p in considered if not _paths_within(p, scope))
        else:
            entry["escaped"] = []
            entry["scope_unknown"] = True
            entry["unverifiable"] = True
        _classify_claim(entry, bool(live_here))

    # Unregistered work: live or dirty in this repository with no writer claim.
    for tree in worktrees:
        path = tree["path"]
        if path in claimed_worktrees:
            continue
        live_here = [a for a in live_by_path.get(path, [])
                     if a["liveness"] == "ACTIVE"]
        others = [a for a in live_by_path.get(path, [])
                  if a["liveness"] != "ACTIVE"]
        status = git.probe_status(path)
        status_items = _probe_items(status, "status")
        entry = {
            "kind": DISCOVERED_UNREGISTERED,
            "status": "discovered_unregistered",
            "worktree": path,
            "branch": tree.get("branch", ""),
            "head": tree.get("head", ""),
            "detached": tree.get("detached", False),
            "live": bool(live_here),
            "agents": [_agent_identity(a) for a in live_here + others],
            "scope_known": True,
        }
        if status_items is None:
            entry.update(kind=UNKNOWN, status="unknown", unverifiable=True,
                         detail=_diagnostic(status))
            entry["changes"] = []
            entry["touched"] = []
            findings.append(entry)
            continue
        changes = git.touched_paths(status_items)
        entry["changes"] = changes
        entry["status_codes"] = sorted({
            "%s%s" % (c.get("index", "?"), c.get("worktree", "?"))
            for c in status_items})
        digest = git.probe_dirty_digest(path)
        canonical_digest = _canonical_probe(digest, "dirty_digest")
        if canonical_digest is not None:
            # Read ONLY the canonical copy, never the caller's object.
            entry["dirty_digest"] = canonical_digest["digest"]
        else:
            entry["dirty_digest_unknown"] = True
            entry["unverifiable"] = True
            entry["detail"] = _diagnostic(digest)
        if baseline:
            committed_items = _probe_items(
                git.probe_committed_diff(path, baseline), "committed")
            if committed_items is not None:
                entry["committed"] = committed_items
            else:
                entry["committed_unknown"] = True
                entry["unverifiable"] = True
        else:
            entry["committed_unknown"] = True
        entry["touched"] = sorted(set(changes)
                                  | set(entry.get("committed") or []))
        if any(a.get("liveness") == UNKNOWN_LIVENESS for a in others):
            entry["worker_state_unknown"] = True
            entry["unverifiable"] = True
        # Distinguish a completed released claim's clean preserved source from
        # active/unknown work. This is diagnostic only: the finding keeps its
        # blocking kind until an explicit, fingerprint-bound historical
        # disposition clears it, and an unverifiable worktree is never labelled.
        released = released_source_provenance(conn, entry)
        if released is not None:
            entry["released_source"] = released
            entry["status"] = RELEASED_UNMERGED
        if not live_here and not entry["touched"] and not others \
                and not entry.get("committed_unknown") \
                and not entry.get("dirty_digest_unknown") \
                and not entry.get("unverifiable"):
            # A quiet idle worktree with no changes, no committed divergence,
            # no worker of any state AND no unreadable evidence is not
            # discovery. "Nothing to report" and "nothing could be reported"
            # are different claims, and only the first stays quiet.
            continue
        findings.append(entry)

    stamp_findings(findings)
    return {
        "ok": True,
        "status": "ok",
        "repo": repo,
        "identity": identity,
        "baseline": baseline,
        "baseline_warnings": canonical["baseline_warnings"],
        "occupancy_unknown": occupancy_unknown,
        "findings": findings,
        "probable_overlaps": probable_overlaps(conn, identity),
        "errors": [],
    }


# --------------------------------------------------------------------------
# The write gate
# --------------------------------------------------------------------------

def scope_is_decidable(work, spec: ClaimSpec) -> tuple[bool, str]:
    """Whether a diff can be proven contained in this claim's scope.

    Path scope is decidable. Component scope is not: a component id has no
    mapping to filesystem paths, so no diff can be shown to stay inside it. A
    write declared only by component therefore cannot be gated and must say so
    rather than pass as if it had been checked.
    """
    paths = _unpack(work["paths"]) if work is not None else []
    components = _unpack(work["components"]) if work is not None else []
    if paths:
        return True, ""
    if components:
        return False, ("write scope is declared by component only (%s); path "
                       "scope is required to verify a live diff"
                       % ", ".join(components))
    return False, "write scope is empty"


def _overlapping(finding: dict, spec: ClaimSpec, scope: list[str],
                 worktree_real: str) -> bool:
    """Does this finding actually bear on the claim being gated?

    Blocking is scope-relative. An UNKNOWN finding is the exception: its scope
    could not be read, so there is nothing to compare and no basis for calling
    it disjoint. Unknown scope conservatively overlaps every write.
    """
    if finding.get("kind") == UNKNOWN or finding.get("unverifiable"):
        return True
    if finding.get("worktree") and finding["worktree"] == worktree_real:
        return True
    if spec.branch and finding.get("branch") == spec.branch:
        return True
    if finding.get("worktree_branch") and spec.branch \
            and finding["worktree_branch"] == spec.branch:
        return True
    touched = set(finding.get("changes") or []) \
        | set(finding.get("committed") or [])
    if _paths_overlap(sorted(touched), scope):
        return True
    if finding.get("escaped") and _paths_overlap(finding["escaped"], scope):
        return True
    if finding.get("committed_unknown") or finding.get("scope_unknown"):
        return True
    return False


def drifted_findings(findings: list[dict]) -> list[dict]:
    """Findings whose worktree is no longer on the branch the claim declared.

    Fail-closed on purpose. A worktree moved to another branch at the same HEAD
    looks identical to an untouched one: same commit, clean tree, same touched
    paths. The only evidence that the claim now describes somewhere nobody
    reviewed is the branch itself.
    """
    return [f for f in findings if f.get("branch_drift")]


def _candidate_checkout_refusal(spec: ClaimSpec, evidence: dict,
                                identity: str, refuse) -> dict | None:
    """Refuse a prospective claim whose declared checkout the evidence denies.

    Checks, in order of how badly a wrong answer misleads: the declared
    checkout exists; it is a worktree of THIS repository; it is not detached
    when the claim declares a branch; and its actual attached branch equals the
    declared branch exactly.

    A claim that declares no checkout is not checked here: it has no location
    to verify, and the ledger's conflict matrix already covers identity without
    one.
    """
    if not spec.worktree:
        return None
    real = os.path.realpath(spec.worktree)
    if not os.path.isdir(real):
        return refuse(NEEDS_RECONCILIATION,
                      errors=["declared checkout does not exist: %s" % real],
                      unverifiable=True)
    actual_identity = git.common_dir(real)
    if identity and actual_identity and actual_identity != identity:
        return refuse(NEEDS_RECONCILIATION,
                      errors=["declared checkout belongs to a different "
                              "repository: %s is %s, the claim is about %s"
                              % (real, actual_identity, identity)],
                      unverifiable=True)
    worktrees = _probe_items(evidence.get("worktrees"), "worktrees")
    if worktrees is None:
        return refuse(NEEDS_RECONCILIATION,
                      errors=["declared checkout could not be verified"],
                      unverifiable=True)
    tree = next((t for t in worktrees if t.get("path") == real), None)
    if tree is None:
        return refuse(NEEDS_RECONCILIATION,
                      errors=["declared checkout is not a worktree of this "
                              "repository: %s" % real],
                      unverifiable=True)
    actual_branch = tree.get("branch") or ""
    if tree.get("detached") or not actual_branch:
        if spec.branch:
            return refuse(CONFLICT,
                          errors=["declared checkout %s is on a detached HEAD, "
                                  "but the claim declares branch %r"
                                  % (real, spec.branch)],
                          conflicts=[{"kind": "candidate_detached",
                                      "against_work": spec.work_key,
                                      "against_owner": spec.owner,
                                      "value": spec.branch,
                                      "actual_branch": "",
                                      "detached": True}])
        return None
    if spec.branch and spec.branch != actual_branch:
        return refuse(CONFLICT,
                      errors=["declared branch %r is not the branch of the "
                              "declared checkout %s, which is on %r"
                              % (spec.branch, real, actual_branch)],
                      conflicts=[{"kind": "candidate_branch",
                                  "against_work": spec.work_key,
                                  "against_owner": spec.owner,
                                  "value": spec.branch,
                                  "actual_branch": actual_branch,
                                  "detached": False}])
    return None


def _is_self_bootstrap(finding: dict, spec: ClaimSpec,
                       worktree_real: str) -> bool:
    """Is this unregistered work just the claimant arriving to register itself?

    Requires ALL of: the finding is the exact worktree this claim declares; the
    claim itself declares a branch; that declared branch matches the
    worktree's actual branch exactly; the checkout is on a known, attached
    branch; a clean tree; and known ZERO committed scope. Occupancy is
    deliberately not part of this test: a supervised session puts a manager and
    a worker in the same worktree before registration, and a second occupant is
    not evidence of a second *writer*.
    """
    if not worktree_real or finding.get("worktree") != worktree_real:
        return False
    if finding.get("changes") or finding.get("committed"):
        return False
    if finding.get("committed_unknown") or finding.get("dirty_digest_unknown"):
        return False
    if not spec.branch:
        return False
    actual = finding.get("branch") or finding.get("worktree_branch") or ""
    if not actual or finding.get("detached"):
        return False
    return spec.branch == actual


def _scope_signature(conn: sqlite3.Connection, spec: ClaimSpec) -> tuple | None:
    """The exact proposal scope a gate validated, for re-checking before insert."""
    work = get_work(conn, spec.run_id, spec.work_key, spec.issue)
    if work is None:
        return None
    return (work["repo"], work["paths"], work["components"],
            work["objective"] or "")


def write_gate(conn: sqlite3.Connection, spec: ClaimSpec, evidence: dict,
               persist_run: str | None = None) -> dict | None:
    """Refuse a WRITE or EXCLUSIVE claim that live evidence does not support.

    Returns ``None`` when the claim may proceed, otherwise a refusal dict.
    READ is proportional and is not gated: reading cannot damage a repository.

    Fail-closed: any required source that could not be read refuses the claim,
    because the registry's job is to stop a write it cannot vouch for. A
    refusal is a normal outcome, not an error: fix the evidence and retry.
    """
    if spec.mode == READ:
        return None
    try:
        return _write_gate_inner(conn, spec, evidence, persist_run)
    except Exception:  # noqa: BLE001 - write_gate never raises
        out = _refusal(NEEDS_RECONCILIATION, spec,
                       errors=["live evidence unavailable"], unverifiable=True)
        out["findings"] = []
        return out


def _write_gate_inner(conn, spec: ClaimSpec, evidence, persist_run) -> dict | None:
    def refuse_early(status: str, repo: str, errors: list[str], **kwargs) -> dict:
        out = _refusal(status, spec, repo=repo, errors=errors, **kwargs)
        out["findings"] = []
        out["reconciled_at"] = now()
        return out

    canonical = _canonical_evidence(evidence)

    # A writing claim must be bound to a checkout and a branch. An unbound
    # writer cannot be placed, so it cannot be vouched for; it is refused here,
    # in the one shared gate consulted by acquisition, resume, preflight and
    # handoff. READ is proportional and stays unbound.
    missing = [name for name, value in (("branch", spec.branch),
                                        ("worktree", spec.worktree))
               if not value]
    if missing:
        return refuse_early(
            NEEDS_SCOPE, canonical["repo"],
            ["a %s claim must bind a %s; an unbound writer cannot be vouched "
             "for" % (spec.mode, " and a ".join(missing))],
            unverifiable=True)

    # Evidence must pass the one aggregate decision, not just carry ok=True.
    if not canonical["ok"]:
        return refuse_early(
            NEEDS_RECONCILIATION, canonical["repo"],
            ["live evidence unavailable"], unverifiable=True)

    # A verified, nonempty repository identity and an EXACT worktree binding are
    # required. An unverified writer is never accepted.
    if not canonical["repo"] or not canonical["identity"]:
        return refuse_early(
            NEEDS_RECONCILIATION, canonical["repo"],
            ["live evidence lacks a verified repository identity"],
            unverifiable=True)
    if canonical["worktree"] != spec.worktree:
        return refuse_early(
            NEEDS_RECONCILIATION, canonical["repo"],
            ["live evidence is not bound to the declared worktree"],
            unverifiable=True)

    work = get_work(conn, spec.run_id, spec.work_key, spec.issue)
    decidable, why = scope_is_decidable(work, spec)
    if not decidable:
        return refuse_early(
            NEEDS_SCOPE, (work["repo"] if work is not None else ""), [why],
            unverifiable=True)

    proposed_identity = git.normalize_repo(work["repo"]) if work is not None else ""
    evidence_identity = canonical["identity"]
    if proposed_identity != evidence_identity:
        return refuse_early(
            NEEDS_RECONCILIATION, proposed_identity,
            ["live evidence describes a different repository"], unverifiable=True)

    report = reconcile(conn, canonical)
    if not report.get("ok"):
        # Reconciliation could not vouch for the live state; never proceed on
        # an empty finding set produced by a failed reading.
        return refuse_early(
            NEEDS_RECONCILIATION, report.get("repo", ""),
            ["reconciliation could not vouch for live evidence"],
            unverifiable=True)
    findings = report["findings"]
    if persist_run:
        persist_findings(conn, persist_run, report)

    def refuse(status: str, **kwargs) -> dict:
        out = _refusal(status, spec, repo=report["repo"], **kwargs)
        out["findings"] = findings
        out["reconciled_at"] = now()
        return out

    def overlaps(finding: dict) -> bool:
        return _overlapping(finding, spec, _unpack(work["paths"]) if work else [],
                            os.path.realpath(spec.worktree) if spec.worktree else "")

    unknown = [f for f in findings
               if f.get("unverifiable") or f["kind"] == UNKNOWN]
    if unknown:
        return refuse(
            NEEDS_RECONCILIATION,
            errors=["live state could not be read for: %s"
                    % ", ".join(sorted({f.get("worktree", "?")
                                        for f in unknown}))],
            unverifiable=True)

    drifted = [f for f in drifted_findings(findings) if overlaps(f)]
    if drifted:
        return refuse(
            CONFLICT,
            errors=["claim branch drift: %s"
                    % "; ".join("%s (%s claim %s)"
                                % (f["branch_drift"], f.get("owner", "?"),
                                   f.get("claim_id")) for f in drifted)],
            conflicts=[{
                "kind": "branch_drift",
                "against_run": f.get("run_id", ""),
                "against_work": f.get("work_key", ""),
                "against_owner": f.get("owner", ""),
                "against_claim": f.get("claim_id"),
                "declared_branch": f.get("branch", ""),
                "actual_branch": f.get("worktree_branch", ""),
                "detached": bool(f.get("detached")),
            } for f in drifted])

    def refuse_candidate(status: str, errors: list[str], **kwargs) -> dict:
        return refuse_early(status, report["repo"], errors, **kwargs)

    candidate = _candidate_checkout_refusal(spec, canonical, proposed_identity,
                                            refuse_candidate)
    if candidate is not None:
        return candidate

    worktree_real = os.path.realpath(spec.worktree) if spec.worktree else ""
    run_for_disposition = persist_run or spec.run_id

    drift = [f for f in findings
             if f["kind"] == SCOPE_DRIFT and overlaps(f)]
    drift, drift_resolved = _apply_dispositions(conn, run_for_disposition, drift)
    if drift:
        return refuse(
            CONFLICT,
            errors=["scope drift: %s"
                    % "; ".join("%s escaped [%s]"
                                % (", ".join(f["escaped"]),
                                   ",".join(f["scope"])) for f in drift)],
            resolved=drift_resolved,
            conflicts=[{
                "kind": "scope_drift",
                "against_run": f["run_id"], "against_work": f["work_key"],
                "against_owner": f["owner"], "against_claim": f["claim_id"],
                "escaped": f["escaped"]} for f in drift])

    stale = [f for f in findings if f["kind"] == STALE_DIRTY and overlaps(f)]
    stale, stale_resolved = _apply_dispositions(conn, run_for_disposition, stale)
    if stale:
        return refuse(
            CONFLICT,
            errors=["a stale claim still holds uncommitted work: %s"
                    % ", ".join(sorted({f["worktree"] for f in stale}))],
            resolved=stale_resolved,
            conflicts=[{
                "kind": "stale_dirty",
                "against_run": f["run_id"], "against_work": f["work_key"],
                "against_owner": f["owner"], "against_claim": f["claim_id"],
                "worktree": f["worktree"], "changes": f["changes"]}
                for f in stale])

    discovered = [f for f in findings
                  if f["kind"] == DISCOVERED_UNREGISTERED and overlaps(f)]
    discovered, discovered_resolved = _apply_dispositions(
        conn, run_for_disposition, discovered)
    blocking = [d for d in discovered
                if not _is_self_bootstrap(d, spec, worktree_real)]
    if blocking:
        return refuse(
            CONFLICT,
            errors=["unregistered work in scope: %s"
                    % ", ".join(sorted({d["worktree"] for d in blocking}))],
            resolved=discovered_resolved,
            conflicts=[{
                "kind": "discovered_unregistered",
                "worktree": d["worktree"], "branch": d.get("branch", ""),
                "live": d.get("live", False), "changes": d.get("changes", []),
                "committed": d.get("committed", [])} for d in blocking])
    return None


# --------------------------------------------------------------------------
# Claim / preflight / release
# --------------------------------------------------------------------------

def claim(conn: sqlite3.Connection, spec: ClaimSpec,
          observation: RuntimeObservation | None = None,
          persist_run: str | None = None) -> dict:
    """Atomically acquire or resume ``spec``.

    The whole check-then-insert runs in one ``BEGIN IMMEDIATE`` transaction, so
    concurrent conflicting claimants serialize and exactly one is granted.

    Live probes run *before* that transaction opens, so a slow probe never holds
    the registry's write lock. That leaves a documented window: live state can
    change between probe and insert. The ledger guarantee is atomicity of the
    claim record; the live guarantee is a fail-closed best effort at probe time.
    """
    spec.validate()
    gated_scope = None
    if spec.mode != READ:
        probe = evaluate(conn, spec)
        if not probe.get("ok"):
            return probe
        work = get_work(conn, spec.run_id, spec.work_key, spec.issue)
        evidence = gather_evidence(work["repo"], observation, spec.worktree)
        gated_scope = _scope_signature(conn, spec)
        refusal = write_gate(conn, spec, evidence, persist_run)
        if refusal is not None:
            return refusal

    conn.execute("BEGIN IMMEDIATE")
    try:
        if gated_scope is not None and _scope_signature(conn, spec) != gated_scope:
            conn.execute("ROLLBACK")
            return _refusal(
                NEEDS_SCOPE, spec,
                errors=["the proposed scope changed while the live gate was "
                        "validating it; the gate did not check the scope now "
                        "being acquired. Re-run the claim."])
        result = evaluate(conn, spec)
        if not result["ok"]:
            conn.execute("ROLLBACK")
            return result
        if result["status"] == RESUMED:
            conn.execute("ROLLBACK")
            return result
        cur = conn.execute(
            "INSERT INTO claims (run_id, work_key, issue, owner, mode, branch, "
            "worktree, claimed_at) VALUES (?,?,?,?,?,?,?,?)",
            (spec.run_id, spec.work_key, spec.issue, spec.owner, spec.mode,
             spec.branch, spec.worktree, now()))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    result["status"] = CLAIMED
    result["claim_id"] = cur.lastrowid
    return result


def preflight(conn: sqlite3.Connection, spec: ClaimSpec,
              observation: RuntimeObservation | None = None) -> dict:
    """Report whether ``spec`` would be granted, writing nothing.

    The ledger verdict comes first: a CONFLICT, JOIN_EXISTING or resume mismatch
    names the holding claim and is more precise than anything live evidence
    could add. The gate then applies only to a claim that would actually be
    granted.
    """
    spec.validate()
    result = evaluate(conn, spec)
    if spec.mode != READ and result.get("ok"):
        work = get_work(conn, spec.run_id, spec.work_key, spec.issue)
        evidence = gather_evidence(work["repo"], observation, spec.worktree)
        refusal = write_gate(conn, spec, evidence)
        if refusal is not None:
            return refusal
    if result["status"] == CLAIMED:
        result["status"] = PREFLIGHT
    return result


def release(conn: sqlite3.Connection, run_id: str, owner: str,
            work_key: str | None = None, issue: str = "",
            outcome: str = "abandon", released_head: str = "") -> dict:
    """End one claim, or every open claim of ``owner`` when ``work_key`` is None.

    ``released_head`` is the exact terminal commit of the work being released,
    and it is the only thing that later lets a successor prove lineage. It is
    recorded for ``complete`` only, must be an exact 40-character commit id, and
    is optional. Claims never expire on their own; this explicit act is the
    only exit.
    """
    if not run_id.strip() or not owner.strip():
        raise RegistryError("run id and owner are required")
    if outcome not in ("complete", "abandon"):
        raise RegistryError("outcome must be complete or abandon: %r" % outcome)
    head = (released_head or "").strip().lower()
    if head:
        if outcome != "complete":
            raise RegistryError(
                "a released head is lineage evidence and is recorded for "
                "outcome=complete only; %r would let abandoned history look "
                "like a finished predecessor" % outcome)
        if not git.HEAD_RE.fullmatch(head):
            raise RegistryError(
                "released head must be an exact 40-character commit id: %r"
                % released_head)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if work_key:
            rows = conn.execute(
                "SELECT claim_id FROM claims WHERE run_id=? AND work_key=? "
                "AND issue=? AND owner=? AND released_at IS NULL",
                (run_id, work_key, issue, owner)).fetchall()
        else:
            rows = conn.execute(
                "SELECT claim_id FROM claims WHERE run_id=? AND owner=? "
                "AND released_at IS NULL", (run_id, owner)).fetchall()
        if not rows:
            conn.execute("ROLLBACK")
            return {"ok": False, "status": ERROR, "released": [],
                    "released_head": "",
                    "errors": ["no open claim for owner %r%s"
                               % (owner, (" work %r" % work_key)
                                  if work_key else "")]}
        stamp = now()
        for row in rows:
            conn.execute(
                "UPDATE claims SET released_at=?, outcome=?, released_head=? "
                "WHERE claim_id=?",
                (stamp, outcome, head, row["claim_id"]))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"ok": True, "status": RELEASED, "outcome": outcome,
            "released": [row["claim_id"] for row in rows],
            "released_at": stamp, "released_head": head}


# --------------------------------------------------------------------------
# Explicit ownership handoff
# --------------------------------------------------------------------------

def _holding_snapshot(conn: sqlite3.Connection, spec: ClaimSpec,
                      owner: str) -> dict | None:
    row = conn.execute(
        "SELECT c.*, w.paths AS paths, w.components AS components, "
        "w.repo AS repo FROM claims c JOIN works w "
        "  ON w.run_id=c.run_id AND w.work_key=c.work_key AND w.issue=c.issue "
        "WHERE c.run_id=? AND c.work_key=? AND c.issue=? AND c.owner=? "
        "  AND c.released_at IS NULL ORDER BY c.claim_id DESC LIMIT 1",
        (spec.run_id, spec.work_key, spec.issue, owner)).fetchone()
    return dict(row) if row is not None else None


def _same_holding(before: dict | None, after: dict | None) -> bool:
    if before is None or after is None:
        return False
    return before["claim_id"] == after["claim_id"]


def handoff(conn: sqlite3.Connection, run_id: str, work_key: str,
            from_owner: str, to_owner: str, authority: str, issue: str = "",
            rationale: str = "",
            observation: RuntimeObservation | None = None) -> dict:
    """Move one open claim from one owner to another, atomically.

    The release, the acquire and the audit record all happen in ONE
    transaction, so a transfer cannot be committed without its provenance. An
    audit failure rolls the whole thing back.

    A WRITE or EXCLUSIVE handoff is ALWAYS gated fail-closed, exactly like a
    WRITE claim. Supplying no runtime observation does not skip the gate: it
    makes occupancy unknown and the transfer is refused with
    ``NEEDS_RECONCILIATION`` while the incumbent stays open. A READ handoff is
    proportional and is not gated. ``authority`` is caller attribution, not
    authentication.

    The new owner is evaluated against the claims that will remain, so a
    handoff cannot smuggle a claim past a conflict. Only this explicit call
    moves a claim; similarity is never an input.
    """
    for label, value in (("run id", run_id), ("work key", work_key),
                         ("from owner", from_owner), ("to owner", to_owner)):
        if not (value or "").strip():
            raise RegistryError("%s is required for a handoff" % label)
    if not (authority or "").strip():
        raise RegistryError(
            "a handoff requires an accountable authority; this is caller "
            "attribution, not authentication")
    if from_owner.strip() == to_owner.strip():
        raise RegistryError("handoff target already owns the claim")

    spec = ClaimSpec(run_id=run_id, work_key=work_key, owner=to_owner.strip(),
                     mode=READ, issue=issue)
    spec.validate()

    def refusal(status: str, errors: list[str], **extra) -> dict:
        out = _refusal(status, spec, errors=errors, **extra)
        out["released"] = []
        out["from_owner"] = from_owner.strip()
        out["to_owner"] = to_owner.strip()
        out["view"] = "handoff"
        return out

    snapshot = _holding_snapshot(conn, spec, from_owner.strip())
    if snapshot is None:
        return refusal(
            ERROR,
            ["%r holds no open claim on %s/%s; nobody was transferred"
             % (from_owner.strip(), run_id, work_key)])

    holding_mode = (snapshot["mode"] or "").strip().upper()
    if holding_mode in (WRITE, EXCLUSIVE):
        # ALWAYS gate a writing handoff. Omitting the runtime observation does
        # NOT skip the gate: gather_evidence then reports occupancy UNKNOWN and
        # write_gate refuses with NEEDS_RECONCILIATION, so the incumbent claim
        # stays open and nothing is transferred. READ is proportional and is
        # not gated (it writes nothing).
        evidence = gather_evidence(snapshot["repo"], observation,
                                   snapshot["worktree"])
        gated = write_gate(conn, replace(spec, mode=snapshot["mode"],
                                         branch=snapshot["branch"],
                                         worktree=snapshot["worktree"]),
                           evidence)
        if gated is not None:
            status = gated["status"]
            if status == CONFLICT and any(
                    (c or {}).get("kind") == "branch_drift"
                    for c in (gated.get("conflicts") or [])):
                # A checkout that moved branch cannot be vouched for, so the
                # transfer needs reconciliation rather than a permanent
                # conflict verdict. The incumbent is untouched either way.
                status = NEEDS_RECONCILIATION
            out = refusal(status, gated.get("errors", []),
                          conflicts=gated.get("conflicts", []))
            out["mode"] = snapshot["mode"]
            out["findings"] = gated.get("findings", [])
            return out

    conn.execute("BEGIN IMMEDIATE")
    try:
        current = _holding_snapshot(conn, spec, from_owner.strip())
        if not _same_holding(snapshot, current):
            conn.execute("ROLLBACK")
            return refusal(
                CONFLICT,
                ["the claim to transfer changed while it was being reviewed "
                 "(expected claim %s, found %s); refusing to transfer a "
                 "successor nobody inspected"
                 % (snapshot["claim_id"],
                    current["claim_id"] if current else None)])
        holding = current
        if get_open_claim(conn, run_id, work_key, issue, spec.owner) is not None:
            conn.execute("ROLLBACK")
            return refusal(
                ERROR, ["%r already holds an open claim on %s/%s"
                        % (spec.owner, run_id, work_key)])
        # Release first so the transfer target is judged against the claims
        # that will remain, not against the claim it is taking over.
        conn.execute(
            "UPDATE claims SET released_at=?, outcome='handoff' WHERE claim_id=?",
            (now(), holding["claim_id"]))
        target = replace(spec, mode=holding["mode"], branch=holding["branch"],
                         worktree=holding["worktree"], owner=spec.owner)
        verdict = evaluate(conn, target)
        if not verdict["ok"]:
            conn.execute("ROLLBACK")
            verdict.setdefault("errors", []).append(
                "handoff refused: %r could not hold %s/%s"
                % (spec.owner, run_id, work_key))
            verdict["released"] = []
            verdict["view"] = "handoff"
            verdict["from_owner"] = from_owner.strip()
            return verdict
        cur = conn.execute(
            "INSERT INTO claims (run_id, work_key, issue, owner, mode, branch, "
            "worktree, claimed_at) VALUES (?,?,?,?,?,?,?,?)",
            (target.run_id, target.work_key, target.issue, target.owner,
             target.mode, target.branch, target.worktree, now()))
        record_observation(conn, run_id, work_key, HANDOFF_KIND, {
            "from_owner": from_owner.strip(),
            "to_owner": target.owner,
            "authority": authority.strip(),
            "rationale": (rationale or "").strip(),
            "released_claim": holding["claim_id"],
            "new_claim": cur.lastrowid,
            "live_gate": bool(observation is not None),
        })
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"ok": True, "status": HANDED_OFF, "run_id": run_id,
            "work_key": work_key, "issue": issue,
            "from_owner": from_owner.strip(), "to_owner": target.owner,
            "mode": target.mode, "branch": target.branch,
            "worktree": target.worktree, "authority": authority.strip(),
            "rationale": (rationale or "").strip(),
            "released": [holding["claim_id"]], "claim_id": cur.lastrowid}


# --------------------------------------------------------------------------
# Evidence fingerprints and explicit supervisor dispositions
# --------------------------------------------------------------------------

def finding_key(finding: dict) -> str:
    """Stable identity of a finding, across reconciliations and processes."""
    claim_id = finding.get("claim_id")
    if claim_id is not None:
        return "claim:%s" % claim_id
    return "%s:%s" % (finding.get("kind", ""), finding.get("worktree", ""))


def finding_fingerprint(finding: dict) -> str:
    """SHA-256 over exactly the evidence a disposition was made against.

    Everything that decides whether this is still the same finding goes in, so
    a decision recorded against one state cannot survive into another: HEAD and
    both branches; the real status codes and a digest of the uncommitted bytes;
    sorted live agent identity; and the touched scope. Committed scope that
    could not be established is recorded as unknown rather than omitted, so a
    decision made before a baseline existed cannot match a later reading.

    There is deliberately no timestamp or wall clock here. A decision is
    invalidated by changed evidence, never by the passage of time.
    """
    agents = set()
    for record in (list(finding.get("workers") or [])
                   + list(finding.get("agents") or [])):
        agents.add((
            record.get("agent_id", ""), record.get("worktree", ""),
            record.get("state", ""), record.get("session_id", ""),
            record.get("liveness", "")))
    payload = {
        "kind": finding.get("kind", ""),
        "status": finding.get("status", ""),
        "claim_id": finding.get("claim_id"),
        "owner": finding.get("owner", ""),
        "worktree": finding.get("worktree", ""),
        "head": finding.get("head", ""),
        "declared_branch": finding.get("branch", ""),
        "actual_branch": finding.get("worktree_branch", ""),
        "detached": bool(finding.get("detached")),
        "scope": sorted(finding.get("scope") or []),
        "components": sorted(finding.get("components") or []),
        "git_status": sorted(finding.get("status_codes") or []),
        "dirty_digest": finding.get("dirty_digest", ""),
        "committed_unknown": bool(finding.get("committed_unknown")),
        "scope_unknown": bool(finding.get("scope_unknown")),
        "touched": sorted(
            set(finding.get("changes") or [])
            | set(finding.get("committed") or [])
            | set(finding.get("escaped") or [])
            | set(finding.get("touched") or [])),
        "agents": sorted(list(a) for a in agents),
    }
    try:
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          allow_nan=False)
    except (ValueError, TypeError):
        # A non-finite number cannot be fingerprinted, so it is refused rather
        # than silently hashed: two different NaN states must not share a
        # fingerprint that a disposition could bind to.
        raise RegistryError(
            "a finding contains a value that cannot be fingerprinted")
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def stamp_findings(findings: list[dict]) -> list[dict]:
    """Attach identity and fingerprint to every finding, in place."""
    for finding in findings:
        finding["finding_key"] = finding_key(finding)
        finding["fingerprint"] = finding_fingerprint(finding)
    return findings


def record_disposition(conn: sqlite3.Connection, run_id: str, finding: dict,
                       disposition: str, authority: str, rationale: str,
                       target_owner: str = "",
                       expected_fingerprint: str = "") -> dict:
    """Record one explicit, accountable decision about one observed finding.

    Enforced here rather than trusted from the caller: the fingerprint is
    RECOMPUTED from the finding, never read from it; ``expected_fingerprint``,
    when given, must equal the recomputed value; ``rationale`` and
    ``authority`` are required; and UNKNOWN / unverifiable findings are refused
    outright. ``authority`` is caller attribution, NOT authentication: the
    registry records who claimed it, not who is really there.

    This path never sets the durable ``verified_historical`` proof bit, so a
    decision recorded through it is caller-attributed and stays run-local. Only
    :func:`record_historical_disposition`, which re-verifies a released source
    against the ledger first, marks a decision as reusable.
    """
    return _record_disposition(
        conn, run_id, finding, disposition, authority, rationale, target_owner,
        expected_fingerprint, verified_historical=False)


def _record_disposition(conn: sqlite3.Connection, run_id: str, finding: dict,
                        disposition: str, authority: str, rationale: str,
                        target_owner: str, expected_fingerprint: str,
                        *, verified_historical: bool) -> dict:
    """Shared disposition writer; ``verified_historical`` is never caller input."""
    if not run_id.strip():
        raise RegistryError("run id is required to record a disposition")
    if disposition not in DISPOSITIONS:
        raise RegistryError(
            "disposition must be one of %s: %r"
            % ("|".join(DISPOSITIONS), disposition))
    if not (authority or "").strip():
        raise RegistryError(
            "a disposition requires an accountable authority; this is caller "
            "attribution, not authentication")
    if not (rationale or "").strip():
        raise RegistryError(
            "a disposition requires a rationale; an unexplained decision "
            "cannot be reviewed by the next session")
    if finding.get("kind") == UNKNOWN or finding.get("unverifiable") \
            or finding.get("committed_unknown") \
            or finding.get("scope_unknown") \
            or finding.get("dirty_digest_unknown"):
        raise RegistryError(
            "UNKNOWN findings cannot be dispositioned: scope that could not be "
            "read cannot be judged historical, independent or joinable")
    key = finding.get("finding_key") or finding_key(finding)
    fingerprint = finding_fingerprint(finding)
    if expected_fingerprint and expected_fingerprint != fingerprint:
        raise RegistryError(
            "refusing to record a decision against evidence that has changed: "
            "reviewed fingerprint %s, current %s"
            % (expected_fingerprint[:12], fingerprint[:12]))
    stamp = now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO dispositions (run_id, finding_key, fingerprint, "
            "disposition, authority, rationale, target_owner, created_at, "
            "verified_historical) VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, key, fingerprint, disposition, authority.strip(),
             rationale.strip(), (target_owner or "").strip(), stamp,
             1 if verified_historical else 0))
        record_observation(conn, run_id, "", DISPOSITION_KIND, {
            "finding_key": key, "fingerprint": fingerprint,
            "kind": finding.get("kind", ""), "disposition": disposition,
            "authority": authority.strip(), "rationale": rationale.strip(),
            "target_owner": (target_owner or "").strip()})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"ok": True, "status": RESOLVED, "run_id": run_id,
            "finding_key": key, "fingerprint": fingerprint,
            "kind": finding.get("kind", ""), "disposition": disposition,
            "authority": authority.strip(), "rationale": rationale.strip(),
            "target_owner": (target_owner or "").strip(),
            "clears_block": disposition in CLEARING_DISPOSITIONS,
            "verified_historical": bool(verified_historical),
            "disp_id": cur.lastrowid, "resolved_at": stamp}


def record_historical_disposition(conn: sqlite3.Connection, run_id: str,
                                  finding: dict, authority: str,
                                  rationale: str,
                                  expected_fingerprint: str = "") -> dict:
    """Record an evidence-bound historical disposition, or refuse.

    Unlike :func:`record_disposition`, the clearance is NOT granted by the
    caller's ``authority`` string or by ``run_id``: both remain attribution and
    the namespace the successor's write gate will look the decision up under.
    The Registry first re-verifies, against its own durable ledger, that the
    finding is the clean preserved-unmerged leftover of a completed, released
    claim (:func:`released_source_provenance`). A finding the ledger cannot
    vouch for is refused outright, so no self-attributed provenance, run id or
    copied annotation can waive a block: the block stands and the refusal is the
    diagnostic.

    When it does verify, the decision is a normal fingerprint-bound disposition
    that also carries the durable ``verified_historical`` proof bit, so a later,
    independent run may reuse it for the same exact leftover. Any change to
    HEAD, branch, status, digest or scope withdraws it, and it is withdrawn the
    moment the source stops re-verifying. It suppresses only the block; it
    grants no claim, scope, release, push, merge or deployment right.
    """
    if not isinstance(run_id, str) or not run_id.strip():
        raise RegistryError("run id is required to record a disposition")
    provenance = released_source_provenance(conn, finding)
    if provenance is None:
        raise RegistryError(
            "no durable released-claim evidence verifies this finding as "
            "historical leftover; refusing to waive a block the ledger cannot "
            "vouch for")
    result = _record_disposition(
        conn, run_id, finding, DISPOSITION_HISTORICAL, authority, rationale, "",
        expected_fingerprint, verified_historical=True)
    result["released_source"] = provenance
    return result


def find_disposition(conn: sqlite3.Connection, run_id: str,
                     finding: dict) -> dict | None:
    """The newest disposition matching this finding's exact evidence state.

    Keyed on run, finding identity AND recomputed fingerprint, so a caller
    cannot make a stale decision match by passing its own stamp. A mismatch on
    any of the three means the evidence moved and the decision no longer
    describes what is there now.

    When this run holds no decision of its own, one further source is consulted:
    a historical decision another run recorded through the evidence-checked
    recorder, for the same finding and fingerprint, while the ledger still
    proves the same released leftover. This is the ONLY cross-run reuse, and it
    is re-proved against the current ledger and live evidence on every lookup
    (:func:`_find_reusable_historical_disposition`). A generic caller-attributed
    disposition stays run-local: it is never returned for a different run.
    """
    key = finding.get("finding_key") or finding_key(finding)
    if not run_id or not key:
        return None
    fingerprint = finding_fingerprint(finding)
    row = conn.execute(
        "SELECT * FROM dispositions WHERE run_id=? AND finding_key=? AND "
        "fingerprint=? ORDER BY disp_id DESC LIMIT 1",
        (run_id, key, fingerprint)).fetchone()
    if row is not None:
        return dict(row)
    return _find_reusable_historical_disposition(conn, key, fingerprint, finding)


def _find_reusable_historical_disposition(conn: sqlite3.Connection, key: str,
                                          fingerprint: str,
                                          finding: dict) -> dict | None:
    """A verified historical decision made under another run, or ``None``.

    A decision one run recorded may clear the same preserved-unmerged leftover
    as seen by a later, independent run, but never on the caller's word. Every
    part of the original proof must still hold BEFORE the decision is handed
    back, so reuse cannot outlive the evidence it was made against:

    * the finding must re-verify against the ledger as the clean preserved
      source of exactly one completed, released WRITE/EXCLUSIVE claim in this
      repository identity and this checkout, on the still-attached, declared
      branch, with every committed path inside the released scope
      (:func:`released_source_provenance`) - so a live, dirty, detached, moved,
      unknown, ambiguous, cross-repo or unaccepted source never gets here;
    * the stored decision must bind the exact same finding identity and
      recomputed fingerprint (the query below), so changed HEAD, branch, status,
      digest or scope withdraws it;
    * the decision must carry the ``verified_historical`` proof bit, so only a
      decision the evidence-checked recorder made is reusable; a generic
      caller-attributed ``historical`` stays run-local.

    Anything short of all three returns ``None`` and the finding keeps blocking.
    Reuse only suppresses the block: it releases no claim, grants no scope, and
    confers no execution, release, push, merge or deployment right.
    """
    if released_source_provenance(conn, finding) is None:
        return None
    row = conn.execute(
        "SELECT * FROM dispositions WHERE finding_key=? AND fingerprint=? "
        "AND disposition=? AND verified_historical=1 "
        "ORDER BY disp_id DESC LIMIT 1",
        (key, fingerprint, DISPOSITION_HISTORICAL)).fetchone()
    return dict(row) if row is not None else None


def list_dispositions(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    return [dict(row) for row in conn.execute(
        "SELECT * FROM dispositions WHERE run_id=? ORDER BY disp_id",
        (run_id,)).fetchall()]


def _apply_dispositions(conn: sqlite3.Connection, run_id: str,
                        findings: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split blocking findings into still-blocking and explicitly cleared.

    Only ``historical`` and ``independent`` clear. ``join`` and ``handoff`` are
    recorded and reported, but the finding keeps blocking, because the action
    they name has not happened yet. A disposition suppresses the BLOCK only: it
    never releases another claim, grants this one, or moves ownership.
    """
    keep: list[dict] = []
    applied: list[dict] = []
    for finding in findings:
        found = find_disposition(conn, run_id, finding)
        if found is None or found["disposition"] not in CLEARING_DISPOSITIONS:
            keep.append(finding)
            continue
        applied.append(found)
    return keep, applied


# --------------------------------------------------------------------------
# Advisory similarity (never grants, refuses, releases or transfers)
# --------------------------------------------------------------------------

OVERLAP_THRESHOLD = 0.5
OVERLAP_STOPWORDS = frozenset({
    "and", "the", "for", "with", "from", "into", "not", "add", "adds",
    "adding", "update", "updates", "use", "using", "run", "runs",
})


def objective_tokens(work: dict) -> set[str]:
    """Token set describing a work item's objective.

    Uses the optional objective text when present, and falls back to identity
    tokens (work key, issue, components) otherwise. Paths are excluded: path
    overlap is already decided exactly by the conflict matrix.
    """
    objective = (work.get("objective") or "").strip()
    parts = [objective] if objective else [
        work.get("issue") or "", work.get("work_key") or ""]
    parts += list(work.get("components") or [])
    tokens: set[str] = set()
    for part in parts:
        for raw in re.split(r"[^A-Za-z0-9]+", str(part).casefold()):
            if len(raw) >= 3 and raw not in OVERLAP_STOPWORDS:
                tokens.add(raw)
    return tokens


def lexical_similarity(left: set[str], right: set[str]) -> float:
    """Jaccard similarity of two token sets, 0.0 when either is empty."""
    if not left or not right:
        return 0.0
    union = len(left | right)
    if not union:
        return 0.0
    return len(left & right) / union


def probable_overlaps(conn: sqlite3.Connection, identity: str,
                      candidate: dict | None = None) -> list[dict]:
    """Work that may be doing the same job, as a review prompt.

    Advisory only, by construction: this function reads and reports. It never
    touches ``claims``, so no amount of lexical similarity can grant a claim,
    refuse one, release one or move one between owners.
    """
    rows = [row for row in list_active_claims(conn)
            if identity and git.normalize_repo(row.get("repo") or "") == identity]
    pairs: list[dict] = []
    seen: set = set()

    def compare(left: dict, right: dict, left_ref: dict, right_ref: dict) -> None:
        left_tokens = objective_tokens(left)
        right_tokens = objective_tokens(right)
        shared = sorted(left_tokens & right_tokens)
        score = lexical_similarity(left_tokens, right_tokens)
        if score < OVERLAP_THRESHOLD or not shared:
            return
        key = (left_ref["run_id"], left_ref["work_key"], left_ref["issue"],
               right_ref["run_id"], right_ref["work_key"], right_ref["issue"])
        if key in seen:
            return
        seen.add(key)
        pairs.append({
            "kind": "PROBABLE_OVERLAP", "advisory": True,
            "score": round(score, 4), "threshold": OVERLAP_THRESHOLD,
            "shared_tokens": shared, "candidate": left_ref, "holder": right_ref,
            "against_run": right_ref["run_id"],
            "against_work": right_ref["work_key"],
            "against_issue": right_ref["issue"],
        })

    def ref(work: dict, state: str) -> dict:
        return {"run_id": work["run_id"], "work_key": work["work_key"],
                "issue": work["issue"], "state": state,
                "owner": work.get("owner", ""),
                "paths": list(work.get("paths") or []),
                "components": list(work.get("components") or []),
                "objective": work.get("objective") or ""}

    if candidate is not None:
        for row in rows:
            compare(candidate, row, ref(candidate, PROPOSED), ref(row, "ACTIVE"))
        return pairs

    for i, left in enumerate(rows):
        for right in rows[i + 1:]:
            compare(left, right, ref(left, "ACTIVE"), ref(right, "ACTIVE"))
    return pairs


# --------------------------------------------------------------------------
# Read-only queries
# --------------------------------------------------------------------------

def list_active_claims(conn: sqlite3.Connection,
                       run_id: str | None = None) -> list[dict]:
    """Every open claim, joined to its work scope, newest last."""
    sql = ("SELECT c.claim_id, c.run_id, c.work_key, c.issue, c.owner, c.mode, "
           "c.branch, c.worktree, c.claimed_at, w.repo AS repo, "
           "w.paths AS paths, w.components AS components, "
           "w.objective AS objective FROM claims c JOIN works w "
           "  ON w.run_id=c.run_id AND w.work_key=c.work_key AND w.issue=c.issue "
           "WHERE c.released_at IS NULL")
    args: tuple = ()
    if run_id is not None:
        sql += " AND c.run_id=?"
        args = (run_id,)
    sql += " ORDER BY c.claim_id"
    out = []
    for row in conn.execute(sql, args).fetchall():
        record = dict(row)
        record["paths"] = _unpack(row["paths"])
        record["components"] = _unpack(row["components"])
        out.append(record)
    return out


def list_works(conn: sqlite3.Connection,
               run_id: str | None = None) -> list[dict]:
    sql = "SELECT * FROM works"
    args: tuple = ()
    if run_id is not None:
        sql += " WHERE run_id=?"
        args = (run_id,)
    sql += " ORDER BY run_id, work_key, issue"
    out = []
    for row in conn.execute(sql, args).fetchall():
        record = dict(row)
        record["paths"] = _unpack(row["paths"])
        record["components"] = _unpack(row["components"])
        out.append(record)
    return out


# The public message for a corrupt persisted observation. Deliberately fixed
# and identifier-free: the row's ids and payload are database-controlled, and a
# message that echoed them would leak stored content into a caller-visible error.
CORRUPT_OBSERVATION = ("registry contains an observation payload that is not a "
                       "JSON object")


def _reject_json_constant(token: str):
    """Refuse the non-standard ``NaN``/``Infinity`` JSON constants."""
    raise ValueError("non-finite JSON constant is not allowed: %s" % token)


def _raise_registry_error(message: str) -> None:
    """Raise a fixed registry error with no raw input attached."""
    raise RegistryError(message)


def record_observation(conn: sqlite3.Connection, run_id: str, work_key: str,
                       kind: str, payload: dict) -> int:
    """Persist one identifier-only observation. Never stores file content.

    The payload must be a JSON object serializable without non-finite numbers.
    A rejected payload is refused before any write, so corrupt state can never
    be introduced through this path. On rejection every raw-bearing local
    (payload, encoded, the connection) is cleared before the error is raised,
    so a traceback frame cannot carry the payload or reach it through the
    connection.
    """
    if type(payload) is not dict:
        conn = run_id = work_key = kind = payload = None
        _raise_registry_error("an observation payload must be a JSON object")
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                             allow_nan=False)
    except Exception:  # noqa: BLE001 - a hostile payload cannot escape
        encoded = None
    if encoded is None:
        conn = run_id = work_key = kind = payload = encoded = None
        _raise_registry_error(
            "an observation payload must be JSON-serializable with no "
            "non-finite numbers")
    cur = conn.execute(
        "INSERT INTO observations (run_id, work_key, kind, payload, observed_at) "
        "VALUES (?,?,?,?,?)",
        (run_id, work_key, kind, encoded, now()))
    return cur.lastrowid


def _raise_registry_corrupt() -> None:
    """Raise the fixed corrupt-observation error with no raw input attached."""
    raise RegistryCorruptError(CORRUPT_OBSERVATION)


def list_observations(conn: sqlite3.Connection, run_id: str,
                      work_key: str | None = None) -> list[dict]:
    sql = "SELECT * FROM observations WHERE run_id=?"
    args: list = [run_id]
    if work_key is not None:
        sql += " AND work_key=?"
        args.append(work_key)
    sql += " ORDER BY obs_id"
    rows = conn.execute(sql, args).fetchall()
    out = []
    for row in rows:
        # Validate the payload BEFORE building any metadata, so a corrupt row's
        # bytes are never copied into a local that outlives the check.
        try:
            parsed = json.loads(row["payload"],
                                parse_constant=_reject_json_constant)
        except (ValueError, TypeError):
            parsed = None
        if not isinstance(parsed, dict):
            # Clear every raw local: identifiers, query, rows (including any
            # prior valid secret-bearing row), the current row, partially
            # parsed output, the accumulated output, and the connection through
            # which a row's payload is reachable. Then raise from a helper with
            # no active exception, so neither traceback frames nor
            # __cause__/__context__ can carry the payload.
            conn = None
            run_id = None
            work_key = None
            sql = None
            args = None
            rows = None
            row = None
            parsed = None
            out = None
            record = None
            _raise_registry_corrupt()
        record = dict(row)
        record["payload"] = parsed
        out.append(record)
    return out


def persist_findings(conn: sqlite3.Connection, run_id: str,
                     report: dict) -> int:
    """Record the whole reconciliation as one replaceable snapshot."""
    if not run_id.strip():
        raise RegistryError("run id is required to persist reconciliation")
    return record_observation(conn, run_id, "", RECONCILIATION, {
        "repo": report.get("repo", ""),
        "identity": report.get("identity", ""),
        "baseline": report.get("baseline", ""),
        "occupancy_unknown": bool(report.get("occupancy_unknown")),
        "findings": report.get("findings", []),
        "errors": report.get("errors", []),
    })
