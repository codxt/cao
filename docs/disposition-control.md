# Disposition control (`cao-disposition`)

`cao-disposition` is a standalone, purpose-specific console command. It is not
part of the ordinary `cao` product CLI, does not change that CLI, and does not
replace the Registry, the team owner, or the historical-disposition contract
already documented in `docs/team-execution.md`.

It exists for one narrow case: a registered team is immutably **PAUSED**, its
next writer dispatch is blocked by a small set of live worktree conflicts, and
an operator has separately approved an exact, bounded, independent
run-local disposition for those conflicts.

The command has exactly four verbs and one input:

```
cao-disposition plan      --team-id <registered-team>
cao-disposition authorize --team-id <registered-team>
cao-disposition apply     --team-id <registered-team>
cao-disposition verify    --team-id <registered-team>
```

There is no `--run-id`, no caller-supplied authority string, no grant path or
UID option, no arbitrary worktree list, and no shell command. The run id is
always derived as `cao-team-<team-id>` from the immutable team record.

## Default: UNAVAILABLE

Nothing is authorized by the presence of the command. With no protected grant
on the host, `authorize` and `apply` report explicit `UNAVAILABLE` and make no
change. A command-line flag, an environment variable, a JSON file the CAO user
can write, an instruction in a prompt, or a caller-supplied Python adapter is
never proof of operator approval.

Provisioning is external and manual. CAO never creates the protected directory
or file, never changes its ownership or mode, and never installs a grant.

## The protected grant

The only accepted grant is one root-owned regular file at the fixed path:

```
/etc/cao/disposition-authority.json
```

The directory and file must be owned by uid 0, must not be writable by group or
other, and must not be reached through a symlink. CAO reads the file
descriptor-relative using the existing protected-grant primitive also used by
the intake authority. Every path component below `/` is checked for ownership,
directory type and safe mode; the leaf is opened with no-follow and read through
a pinned descriptor. A foreign owner, relaxed mode, symlink, unreadable path,
oversized file, malformed JSON, duplicate key or non-finite number is refused,
not treated as absent. A genuinely missing leaf is the honest `UNAVAILABLE`
case.

The basename is intentionally different from `intake-authority.json`. One
provisioned file never authorizes both concerns.

### Grant schema

The document must contain exactly these keys:

```json
{
  "schema": "cao.disposition.authority.v1",
  "team_id": "sample-team",
  "plan_digest": "<64 lowercase hex characters>",
  "manifest_digest": "<64 lowercase hex characters>",
  "operator": "operator-principal",
  "issued_at": "2026-01-01T00:00:00Z",
  "expires_at": "2026-01-01T01:00:00Z",
  "revoked": false,
  "action": "run_local_independent",
  "scope": {
    "repo": "/srv/example/repository",
    "paths": ["README.md", "src"]
  },
  "worktrees": [
    {
      "worktree": "/srv/example/worktrees/other",
      "fingerprint": "<64 lowercase hex characters>"
    }
  ]
}
```

Rules enforced by `authorize` and again by `apply`:

* `team_id`, `plan_digest`, `manifest_digest`, `scope` and every
  worktree/fingerprint pair must match the **current** live plan exactly.
* `issued_at` must not be in the future. (`granted_at` is accepted as the
  legacy spelling; a grant must carry exactly one of the two.) `expires_at`
  must follow the issue timestamp, be later than the current time, and stay
  within a 30-day maximum validity window. There is no unbounded grant.
* `revoked` must be `false`.
* `action` must be exactly `run_local_independent`.
* `worktrees` is an exact set: no wildcard, no duplicate path, no duplicate
  pair, and each pair must name a current conflicting worktree with the exact
  current finding fingerprint.
* Every approved worktree must be an unowned, unregistered finding. Work still
  held by an open Registry claim cannot be independently dispositioned.
* The grant may approve a subset of the current conflicts, but every pair it
  names must be present and unchanged.

The grant is not a token that can be replayed later. It binds one plan digest,
including the manifest, repository HEAD and branch, WRITE scope, baseline,
conflict fingerprints, active claims and observed occupancy. Once any of those
change, the grant is stale and both `authorize` and `apply` refuse.

## `plan`

`plan` is read-only and creates no state:

1. Reads the existing immutable team checkpoint through the team owner and
   requires overall status `PAUSED`, desired state `paused`, and only pending or
   complete task slots.
2. Requires one repository, at least one WRITE task, and a non-empty exact WRITE
   scope.
3. Probes live Git worktrees, status, dirty-content digests and committed
   divergence through the existing read-only Git layer.
4. Reads live runtime occupancy through the installed runtime's read-only
   command surface. A missing runtime is `UNAVAILABLE`; occupancy is never
   invented.
5. Reconciles the shared Work Registry **read-only** and runs its read-only
   preflight for already-proposed WRITE work.
6. Prints every conflicting worktree with its exact fingerprint, finding kind,
   status, HEAD, branch, detached flag, dirty digest, status codes, active claim
   and observed agents, plus every active claim and one canonical plan digest.

`plan` refuses an absent team, a non-PAUSED team, a multi-repository manifest,
a team without WRITE scope, an UNKNOWN or unverifiable finding, and an
unavailable runtime or Registry. It never proposes work, claims scope, records
a disposition or starts an agent.

## `authorize`

`authorize` performs the same read-only plan and then validates the protected
grant against it. It prints the authenticated operator, grant fingerprint,
binding digest, approved pairs and conflicts that were not approved. It writes
nothing.

A missing grant is `UNAVAILABLE`. An expired, revoked, malformed, foreign-owned,
mode-relaxed, symlinked, stale-plan or off-scope grant is `REFUSED`.

## `apply`

`apply` does not accept a prior authorization object or a digest supplied by
the caller. It re-reads the protected grant, re-plans live, and then:

1. Opens the shared Registry for writing and starts one `BEGIN IMMEDIATE`
   transaction.
2. Re-reads and revalidates the protected grant under the write lock.
3. Re-plans live inside the transaction and requires the plan digest to equal
   the grant-bound digest exactly.
4. Refuses if any `WRITE` or `EXCLUSIVE` claim is active in the repository, any
   live runtime agent is present, or any source is unknown, ambiguous or moved.
5. Revalidates every approved worktree/fingerprint pair, including a changed
   dirty-content digest, a moved HEAD, a changed branch or a newly detached
   checkout. A dirty or detached finding is admissible only when the exact
   authenticated grant names its exact fingerprint and it has no owner.
6. Inserts every approved `independent` disposition and its existing
   identifier-only observation in that one transaction, with the authenticated
   operator and the plan/grant binding digest in the persisted provenance.
7. Commits all of them, or rolls the entire batch back on any failure.

There is no partial application. An identical replay finds the same run,
finding key, fingerprint and independent disposition; it reports `REPLAYED` and
inserts nothing. A prior-run decision is never reused automatically, a
historical disposition is never recorded by this command, and no `claims` row
is changed.

## `verify`

`verify` is read-only. It re-probes current live state and reports:

* the remaining conflicting worktrees and the current preflight status;
* every current finding resolved through the Registry's own lookup, including
  the disposition kind, operator authority, rationale, timestamp and
  `verified_historical` flag;
* whether a separately governed writer dispatch could now proceed.

It never claims ownership, never starts a team, and never turns a stale or
revoked grant into a change.

## Failure and transaction rules

* Fail closed: unreadable evidence is `UNKNOWN`/`UNAVAILABLE`, never empty or
  clean.
* Unknown findings, unverifiable scope, missing runtime, missing Registry and
  ambiguous worktrees are refused.
* The claim ledger is authoritative and is never edited by this command.
* No deployment, publication, merge, push, credential, policy or permission is
  granted by a disposition.
* The command uses the standard library only and stores no second authority
  ledger.
