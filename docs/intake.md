# Intake

Intake is CAO's inbound gate for operator-submitted items. It accepts one
bounded, strictly-shaped JSON document, admits it **once**, and stores the
result in the operator's private XDG CAO state. An admitted item is a durable
record with an explicit state. Nothing in intake starts, merges, dispatches,
schedules or claims anything, and intake reads and changes no permission or
configuration of any connected external system.

Two standard-library modules carry the whole feature.

| Module | Owns |
|---|---|
| `src/cao/intake.py` | strict input, private durable storage, idempotency, conflict refusal, redaction |
| `src/cao/intake_bridge.py` | the inbound transport contract: marketplace-only, stdin/JSON, no service |

## What an admission is, and is not

An admission is a record of intent. It is **not** a work item and it is **not**
a Work Registry claim:

- The stored record has `record_type = "intake_submission"`, distinct from any
  work item.
- `intake.py` is guarded by `tests/test_boundaries.py`: it may not import
  `runtime`, `policy`, `contracts` or `registry`, so an admitted submission can
  never start, merge or claim anything.
- Nothing here grants authority. Publish, deploy, release and other reserved
  actions remain separately authorized.

## Durable, private storage

- Records live under `$XDG_STATE_HOME/cao/intake/`, one file per submission,
  named by the durable submission id (64 lowercase hex characters).
- The submission id is `sha256(item_id)`: the same item is the same durable id
  across processes and restarts. There is no in-memory cache.
- Directories are `0700` and files `0600`. Only the leaf is created by
  `makedirs`, so the CAO state root above the intake directory is hardened to
  `0700` explicitly as well. Every directory actually created on first submit
  (the CAO state root and the intake directory, and any missing ancestor) has
  its **parent** directory fsynced, so a newly created name cannot be lost by a
  crash before an admission is acknowledged.
- The CAO state root and the intake directory have their **parent** directory
  fsynced on **every** acknowledgement, whether or not this submit created them.
  A present name is not by itself evidence of durability: an earlier attempt can
  create the directory and then fail that exact sync, so a retry that skipped the
  sync whenever the directory already existed would acknowledge an admission
  into a directory a crash can still lose. Re-syncing the existing ancestor is
  the durable evidence that the admitted path survives, and it needs no separate
  pending-durability state that a process restart could drop. A failure to sync
  the CAO-root entry or the intake entry therefore refuses the admission rather
  than acknowledging it — on the first submit and on every retry, so a
  persistent ancestry-sync failure never yields `ADMITTED` or `DUPLICATE`.
- A record is written to a private temp file, fsynced, then hard-linked into
  place and the containing directory fsynced **before** the admission is
  acknowledged. `os.link` is atomic and fails closed on an existing path, so
  two concurrent submitters can neither overwrite each other nor duplicate a
  record. A failure after the link is never acknowledged; an honest retry finds
  the record and reconciles it as a duplicate, and that duplicate path also
  syncs the directory (propagating a sync failure) so a link that is not yet
  durable is never acknowledged. A failure to sync any required ancestry —
  created or already present — likewise refuses the admission rather than
  acknowledging it, and an honest retry after the sync is restored reconciles.
- The whole write is descriptor-relative. After the CAO state root and the
  intake directory are validated, their descriptors are **pinned**: the temp
  file is created, the record is hard-linked, the temp file is cleaned up and
  the record is read back all relative to the validated intake descriptor, and
  the containing directory is fsynced through that same descriptor. A same-user
  process that swaps a path component for a symlink after validation therefore
  cannot redirect a complete record into a substitute tree, and cannot make a
  read-back agree with a record it never validated.
- Records are read through a strict, bounded reader that refuses duplicate JSON
  keys and non-finite numbers, and that re-derives the id and content
  fingerprint from the stored bytes. `submission_id` must equal
  `sha256(item_id)`, and the canonical fingerprint is recomputed over the stored
  body, id, title, source and evidence refs on **every** `show`, `list` and
  duplicate check. Nested `evidence_refs` must already be exactly canonical in
  the stored bytes — canonicalizing before fingerprinting may not launder an
  added key such as `fingerprint: null`, which is refused (absent and null are
  not equivalent). A corrupt, tampered, malformed or unknown-schema record
  **raises** and is never rounded up to an empty result or a valid record.
- The CAO state root above the intake directory is opened first with
  `O_NOFOLLOW` and required to be a real, owned `0700` directory, and the intake
  directory is then opened relative to that validated descriptor. A symlinked,
  dangling, foreign-owned or world-readable state root fails closed rather than
  being silently followed. Each record is opened with `O_NOFOLLOW` and
  `O_NONBLOCK` (so a FIFO substituted for a record cannot block the open before
  its type is checked) and `fstat`-checked: a symlink, a non-regular file, a
  file with unsafe permissions, or a path owned by another user fails closed.
  Only a real, owned `0700` directory is ever chmod'ed; a symlinked or foreign
  path is refused rather than followed. An absent intake directory is the only
  case that legitimately lists as empty — an existing symlink, non-directory or
  unreadable path raises.

### Idempotency, duplicates and conflicts

Each record stores a content `fingerprint` beside its id:

- Same item, same content, submitted again -> `DUPLICATE` with the **same**
  durable id and fingerprint. One record.
- Same item, **different** content -> `IntakeConflict`. The original is left
  untouched; nothing is merged over it.

## Secret handling

Bodies, titles, sources and evidence references are untrusted, opaque data.
They are stored verbatim after obvious credentials (AWS keys, provider tokens,
private keys, `password:`/`token:` assignments, `Bearer` tokens) are replaced
with `[REDACTED]`, and the record records whether any redaction happened
(`body_redacted`). Only public-safe metadata is reported by `list`; a body is
never reported by `list`.

Redaction consumes the **whole** credential value, never a prefix that could
leave the secret behind as a suffix: a `Bearer` token is redacted before any
assignment rule can see it, so `Authorization: Bearer <token>` becomes
`Authorization: [REDACTED]` and never `Authorization: [REDACTED] <token>`.
Quoted values (`password="an inner space"`, JSON `"token": "..."`) are replaced
whole, and a key quoted with either delimiter is recognised — a single-quoted
`'token': '...'` or `{'password': '...'}` assignment is redacted rather than
slipping past the assignment rules. An `item_id` that is itself credential-shaped
(including a single-quoted assignment such as `'token'='...'`) is refused as
`INTAKE_INVALID` rather than redacted into a different durable identity, and no
raw credential reaches the public view.

## CLI: local submission

```sh
cao intake submit --file submission.json      # admit one strict submission file
cao intake list                              # public-safe metadata (no bodies)
cao intake show <submission-id>              # one full record from private state
cao intake transports                        # the inbound transport contract
```

Exit codes are `0` success, `2` usage error, `3` explicit
`UNAVAILABLE`/conflict.

## Send to CAO: one request entrypoint

`cao request` is the single request entrypoint. A caller sends one bounded
request document, CAO makes **one finite routing decision** and reports it, so
the caller never has to choose between the Intake API and the governed Team
Execution API:

```sh
cao request --item-id ID --text "…"           # a plain-text request
cao request --file manifest.json              # report the governed team route
cao request --file manifest.json --execute    # route into the owner path
cao request --file request.json --dry-run     # classify/validate, no effect
```

### The finite routing decision

Exactly three decisions are emitted, plus a refusal:

| decision | route | meaning | status |
|---|---|---|---|
| `BACKLOG` | `INTAKE` | recorded for later; no execution was requested | `ADMITTED` (or `DUPLICATE` on replay) |
| `INVESTIGATE` | `HOLD` | a human decision or investigation is required before implementation | `PROPOSED` (held) |
| `IMPLEMENT` | `EXECUTION` | clearly-expressed implementation intent: a visible execution candidate | `PROPOSED` while gated, `EXECUTING` only with team evidence |
| — | — | unroutable, invalid or unavailable | `REFUSED` / `UNAVAILABLE` |

Every payload carries `decision`, `route`, `status`, `authority` and an
`execution` block with `requested`, `started`, `gate`, `needs` and `next`.
`execution.requested` says execution was **asked for**; `execution.started` says
it **actually began**. The two are never conflated, and `EXECUTING` is reported
only when a team checkpoint backs it.

For opaque request text the decision rule table is deterministic and
documented; first match wins, no model and no hidden state:

1. it asks for a reserved consequential effect (`merge`, `deploy`, `release`,
   `publish`, `credential`, `permission`, `destructive`) — reusing B6's
   mention-vs-request classifier — or leaves one unprovable → `INVESTIGATE`; a
   mentioned, negated, conditional or already-finished effect is not an asked-for
   action, so it does not hold an otherwise clear implementation request; if
   the classifier cannot read the text at all the request fails **closed** to
   `INVESTIGATE`, never silently to `BACKLOG`;
2. it explicitly defers (`later`, `not now`, `backlog`, `someday`, …) →
   `BACKLOG`;
3. it carries a risk marker (see B3 triage) → `INVESTIGATE`;
4. it is a genuine question → `INVESTIGATE` (a leading prohibition such as
   "do not …" is an imperative limitation, not a question);
5. it carries a work verb **outside a negating directive** → `IMPLEMENT`; the
   work vocabulary is the shared B3 predicate plus a task-router-local set of
   common action verbs it predates (`execute`, `run`, `continue`, `resume`,
   `finish`, `complete`, `proceed`), so "execute … now" and "continue the
   batch" are implementation intent, not backlog. A negative directive
   ("do not implement X", "do not run it", "do not continue") contains no
   positive work and is recorded, never executed;
6. it carries an imperative review/investigation verb **outside a negating
   directive** (`review`, `audit`, `inspect`, `investigate`, `analyze`,
   `assess`, `diagnose`, `compare`, `research`, `check`) → `INVESTIGATE`; a
   request to look at something is a hold for a human decision, not a plain
   backlog note. This rule is evaluated *after* rule 5, so a mixed request that
   names explicit work ("review then implement the fix", "check and continue the
   batch") stays `IMPLEMENT`;
7. otherwise → `BACKLOG`.

A verb in a **quoted span** is a mention, not a directive: a caller quoting an
example or a policy string does not thereby authorize the quoted verb, so the
local work/review predicates run over the text with backtick and double-quoted
spans removed. Both local vocabularies reuse B6's negation scoping exactly (a
contrastive coordinator ends an earlier negation), and neither is applied by
broadening the shared B3 predicate for its other callers.

### One durable request, one effect

Every plain request is admitted through the existing Intake gate exactly once
and idempotently: the same item and content is `DUPLICATE`, the same item with
different content is `INTAKE_CONFLICT`, and a replayed decision never creates a
second record. That durable admission is the **only** persisted effect of the
text route. An `IMPLEMENT` decision makes the request *visible* as an execution
candidate instead of burying it in the backlog; it starts nothing. The request
text, a work verb and an Intake admission are **never** approval.

### One read-only unified review

`cao request --review` is the one local review surface over the requests CAO
already admitted. It derives a bounded view from existing state only — the
Intake record, the B5 resolution, the B6 eligibility/link and the B7 backlog —
and adds no ledger, cache, scheduler, team or worker:

```sh
cao request --review                      # every admitted item, bounded
cao request --review --item-id ID         # exactly one item
```

Each item reports public-safe Intake metadata (ids, title, source, state,
admission time and fingerprint), the deterministic route decision as a
**candidate**, the B5 status and next mover, the B6 eligibility holds and plan
status, and only then the linked team and backlog state. The request body is
never included: `request.body_included` is always `false`, and no body,
redacted body, credential or prompt is echoed.

An `IMPLEMENT` candidate carries its source id and
`execution.requested = true`, but `execution.started`, `worker.started`,
`worker.dispatched` and `approved` are all `false`; the candidate is proposed
intent, never approval and never a running agent. A linked CAO team is shown only
when its provenance is attributable to this exact submission (validated READ
team, immutable manifest citing the submission). Otherwise `team.verdict` is
`UNKNOWN` with no team id. Backlog is attributed only by the queue intent's exact
`submission_id` binding; an unrelated intent is never matched by title, path or
guesswork. If an Intake, team, Registry or queue source is unavailable, corrupt
or above the fixed review bound, the command prints one `UNAVAILABLE` payload
and exits `3` rather than showing a partial or truncated view. `--review` cannot
be combined with `--file`, `--text`, `--execute` or `--dry-run`; that is a usage
error.

The review writes nothing. It opens the Registry read-only when a ledger already
exists and never creates one, never initializes or starts a team, never ticks the
queue and never dispatches an agent.

### The governed team route

A `cao.team.v1` manifest is validated and each resolved work/review route is
independently qualified against the existing Team Execution authorization (the
operator's model allowlist and the approval policy). Without `--execute` the
route is reported read-only:

- a new manifest is `ROUTED` with `next_commands` = the exact existing
  `cao team plan` / `cao team init` / `cao team start` commands (shell-quoted, so
  an unsafe path can never make a printed command ambiguous);
- an already-existing team is reconciled by identity: a team whose task slot is
  **actually running** reports `EXECUTING` with team evidence, a team that is
  only `PENDING` (desired state set, no worker dispatched) is `PROPOSED`, and an
  existing team with a different immutable manifest is
  `TEAM_IDENTITY_CONFLICT` — never replaced or merged. Nothing is ever reported
  `EXECUTING` from a desired state alone.

`--execute` asks CAO to route the manifest into the existing owner path. It adds
**no** authority, records no approval and never dispatches a worker:

- **chat mode** only advances a team whose saved operator admission already
  exists; an absent or unapproved team is `PROPOSED` (`operator_approval`) with
  the exact commands, because the router will not fabricate the operator's own
  `cao team init --approved`;
- **legacy mode** reuses the exact `cao team init`/`cao team start` semantics
  through the same owner API (`team.initialize`/`team.set_desired`); running
  `--execute` is the same operator declaration those commands already record.
  Setting the desired state is intent, not an agent, so the result is `PROPOSED`
  with `execution.started = false` until a task slot actually runs; the named next
  step is the existing daemon's reconcile (`cao team tick --id …`), never a
  duplicate start;
- an exact manifest that names an absent checkout is `PROPOSED`
  (`repository_scope_absent`), never started against a path that cannot exist.

Starting a team only sets its desired state; the existing daemon's own tick
reconciles it through the Work Registry. `--execute` never ticks a team, never
starts an agent, never writes a Registry row and creates no second store.

The entrypoint is a **router**, not an orchestrator. It grants no authority,
bypasses no Work Registry rule, upgrades no privilege, deletes nothing,
auto-merges, pushes or deploys nothing. `--dry-run` validates and classifies
without admitting or starting anything (`--dry-run --execute` is a usage error),
`--execute` without an exact `--file` manifest is a usage error, `--json` is
accepted for compatibility, and an unroutable document is refused
(`UNKNOWN_REQUEST`) rather than guessed.

### What remains gated

Fully automatic implementation is **not** claimed. A first dispatch, a WRITE, a
merge, a deploy, a release, a publication, a credential or a permission change
still require the existing qualified operator authorization (the B6/B7/B8
in-process adapter, the protected standing grant, or the operator's chat
approval). Ambiguous or under-specified requests are held with an actionable
next step rather than guessed.

The original human message establishes **intent**, not an independently
host-authenticated identity and not a broad grant. This entrypoint never invents
an authenticated approval, never records one from text or a CLI flag, and never
relaxes the exact team manifest/model route digest or the Registry writer gates.

Once an authenticated, scope-bound owner approval is available, clear requested
work should not need a second subjective accept/reject decision: the approval
is the decision. The **actual first WRITE dispatch** still needs the existing
approved team and a live Registry/route qualification at dispatch time, because
approval of a scope is not proof that the current writer, source head or claim
state is unchanged. Approval is rechecked only when the scope, the model
route/privacy, the identity or a reserved effect changes. This creates no
standing authority and no automatic permission elevation.

Broader documentation integration is outstanding: the task-centered backlog view
(`cao team backlog`, which groups the immutable team records by initiative and
successor lineage) belongs in `docs/team-execution.md`, which this change
deliberately does not edit because that shared interface doc requires accountable
integration review.

## The inbound bridge (marketplace-only)

The inbound path is a **data** path, not a service. This build runs no custom
MCP server, no webhook receiver, no HTTP endpoint and no other public service.
There is exactly one registered inbound transport:

```
marketplace.chatgpt-business
```

A caller that names any other transport is refused, not quietly served by a
substitute. The contract is public-safe and machine-readable:

```sh
cao intake transports
```

### Tested mechanical workflow

The marketplace app is published in the platform's own marketplace; CAO does
not host or configure it. An already-connected adapter in the operator's own
environment (a local script) receives the app's structured JSON and pipes those
bytes to CAO. The tested, exact flow is:

```sh
# The adapter writes the app's JSON document to CAO's stdin as data.
# It never builds a shell command out of the document.
cat submission.json | cao intake pull --transport marketplace.chatgpt-business

# Or, equivalently, from a file:
cao intake pull --transport marketplace.chatgpt-business --file submission.json
```

CAO answers with its **actual** admission:

```json
{
  "schema": "cao.intake.admission.v1",
  "bridge_schema": "cao.intake.bridge.v1",
  "transport": "marketplace.chatgpt-business",
  "status": "ADMITTED",
  "duplicate": false,
  "submission": {
    "record_type": "intake_submission",
    "submission_id": "…64 hex…",
    "item_id": "…",
    "state": "ADMITTED",
    "fingerprint": "sha256:…",
    "body_redacted": false,
    "admitted_at": "…"
  }
}
```

### Never interpolate untrusted JSON into a shell command

The document is read as bytes and parsed as strict JSON. Nothing read from the
stream is handed to a shell, interpolated into a command line, or executed. A
body full of shell metacharacters (`;`, `$()`, backticks, `|`, `>`, newlines)
is opaque content; it is stored as data. A synthetic test admits such a body and
asserts that no side effect occurs and that the bytes round-trip unchanged.

### Acknowledgement and reconciliation on ambiguity

If a sender loses the answer to an ambiguous send and retries the same item, CAO
collapses it to `DUPLICATE` with the **same** durable id and fingerprint. The
sender reconciles to one record instead of creating a second. A retry with the
same item but different content is refused as a conflict. This is the only
reconciliation CAO performs; it needs no credential and reaches no remote
service.

### Request contract

| Field | Required | Bound |
|---|---|---|
| `schema` | yes | must be `cao.intake.request.v1` |
| `item_id` | yes | printable, space-free, not credential-shaped, <= 200 chars |
| `body` | yes | <= 128 KiB chars |
| `title` | no | <= 300 chars |
| `source` | no | <= 200 chars (default `operator`) |
| `evidence_refs` | no | <= 64 entries, each `{ref, fingerprint?}` |

The whole document is bounded to 256 KiB, must be valid UTF-8, must be a JSON
object, and must not contain duplicate keys or non-finite numbers. Any
violation is an explicit `INTAKE_INVALID`, never a partial admission.

## Fail-closed transports

| Situation | Result |
|---|---|
| No transport named | exit `3`, `NO_INBOUND_TRANSPORT` |
| Unregistered transport (e.g. a custom MCP or a webhook) | exit `3`, `UNSUPPORTED_TRANSPORT` |
| Known but deferred transport | exit `3`, `TRANSPORT_DEFERRED` |

The transport is checked **before** the input is touched: an unsupported or
deferred transport is refused before the `--file` path is opened and before
stdin is consumed, so a refused transport reads nothing and writes nothing.

## Deferred: private GitHub Issue adapter

A private GitHub Issue adapter is **deferred, not implemented**. Reading issues
requires an authenticated remote read (a credential this build must not hold)
and adds a remote egress surface, while the marketplace stdin path already
covers the operator's connected flow. It is reported as an explicit deferred
transport rather than silently missing; no half-working adapter is registered.

## Advisory triage (B3): on-demand, read-only

`cao intake triage` is the on-demand B3 path. It reads one already-admitted
submission and bounded read-only context, and returns **one advisory
disposition** with cited provenance. It is **read-only**: it starts, merges,
dispatches, schedules, claims, sends and deploys nothing, and writes no Registry
row. It is deliberately distinct from the submission (its own private packet
store) and from canonical accepted Company State.

```sh
cao intake triage --id <submission-id>          # advisory packet for one admission
cao intake triage --id <id> --repo /path/repo   # + bounded default-branch Git evidence
cao intake triage --item-id <item-id>           # derive the submission id from the item id
cao intake triage --id <id> --repo /repo --cutoff 2026-10-01
cao intake triage --id <id> --repo /repo --implemented <commit>
```

Exactly five dispositions are permitted: `ASK_CONTEXT`, `ANSWER_CANDIDATE`,
`PROPOSE_WORK`, `HUMAN_REVIEW` and `DENY_OR_REPLAN`. `PROPOSE_WORK` and
`ANSWER_CANDIDATE` are **advisory only**: a proposal may be routed by the B4
compiler (which still grants no authority), and a candidate answer is not
self-authorized.

Read-only context, each with explicit `UNKNOWN`/`UNAVAILABLE` when unavailable:

- the submission itself, through intake's own strict reader (never the
  permissive state reader);
- the submission's declared `evidence_refs`;
- bounded default-branch Git evidence for `--repo`: the resolved default branch,
  the source commit id and its commit time;
- local CAO team summaries and active Work Registry claims, read through the
  Registry's read-only (`mode=ro`) connection — no claim, no mutation. The
  ledger path is resolved by the product config (`CAO_REGISTRY_DB`, then the
  config `registry_db`, then the isolated XDG default), so triage reads the same
  governed ledger the task layer attaches to; it never guesses a path and never
  creates the database.

Load-bearing properties:

- **Deterministic and self-honest.** The disposition comes from a documented rule
  table, never from a model. With no qualified provider this build performs **no
  provider egress** and always yields the deterministic context packet; it never
  pretends an AI decided.
- **No fabricated claims.** Duplicate and already-implemented status are reported
  only with attributable evidence; priority, dependencies and capability hints
  stay `UNKNOWN` when uncertain.
- **Separate, private, idempotent storage.** Advisory packets live under
  `$XDG_STATE_HOME/cao/intake-triage/`, directories `0700` and files `0600`,
  written through the same validated, atomic, fsynced store as admission. The
  packet id is a one-way digest of the evidence bundle, so an identical replay is
  idempotent and different evidence is a distinct packet — never a silent merge.
  The packet **never carries the submission body**.
- **Fail closed.** A stale source is reported stale and is not used as current
  grounding; a symlinked, foreign-owned or unsafe-mode path, a malformed packet,
  a bad ref, cutoff or commit, an unresolvable repository or a hostile body all
  fail closed. The submission body is opaque data: it is inspected only to pick a
  disposition and flag risk, and is never executed or interpolated into a shell.
  The bounded Git observation runs with a minimal sanitized child environment
  (only `PATH`, `HOME`, `TMPDIR` and `GIT_OPTIONAL_LOCKS=0`), so a caller's
  `GIT_DIR` or `GIT_WORK_TREE` cannot redirect it to an unrelated repository.
- **No invented scope.** Intake is single-operator and models no tenant or guest
  principal, so triage invents no cross-scope identity: it reads only the
  operator's own private state and locally available evidence. A source outside
  that — an unresolvable ref, an unverifiable commit, a stale source — fails
  closed rather than being silently adopted.

The full plan for the governed middle — B3 (implemented), B4 routing and B5
resolution (both future plan) — is in
[`docs/intake-roadmap.md`](intake-roadmap.md).

## Governed routing compiler (B4): draft only

`cao intake compile` is the B4 path. It reads one already-persisted B3 advisory
packet whose disposition is **`PROPOSE_WORK`** — and only that — together with a
strictly bounded, operator-supplied **route evidence** file, and compiles one
**immutable, reviewable `cao.team.v1` draft** intended for the **existing** CAO
Team Execution path.

```sh
cao intake compile --packet-id <packet-id> --route-file route.json
cao intake compile --packet-id <packet-id> --route-file route.json --dry-run
```

It is **draft only**. The compiled manifest is `proposed`, carries
`authority = "none"` and `dispatch = false`, and names the explicit owner and
reviewer. B4 compiles and records; it starts, merges, dispatches, schedules,
claims, sends and deploys nothing, writes no Registry row and writes no
canonical Company State. The accountable owner must independently review the
draft and run `cao team init --approved` before any governed dispatch is
considered, and then authorize dispatch separately.

The route file is **evidence, never approval**. It can only supply the accepted
Plan/objective/work package, the required Capability, the domain/repository/path
scope, the performer qualification and the current authority; it cannot
authorize anything. Each load-bearing claim must cite a **source document
anchored to an exact Git identity** (repository, ref, commit id, committer time
and content digest). B4 verifies every citation with read-only Git evidence: the
checkout is readable, the ref resolves to the cited commit id and time, and the
document at that commit matches the cited digest. A free-text `accepted: true`,
an unverifiable digest, a ref that has moved, a missing document or a
credential-shaped value is refused rather than accepted.

That verification establishes only that the cited document **exists at that
commit with that content**. It does **not** establish that a currently-effective
delegation or an operator decision exists: `accepted_at` and the authority grant
are cited evidence, not proof of authority. The draft therefore reports the
authority grant as declared but **not verified** (`verified: false`,
`source_verified: true` — source existence only), keeps `authority = "none"` and
`dispatch = false`, and leaves acceptance to the accountable owner outside Git.
No digest, timestamp or free-text acceptance in the route can assert authority.

Where a precondition cannot be objectively established B4 returns an explicit
`NEEDS_HUMAN_REVIEW` (unverifiable plan, capability or authority evidence; a
manifest with no independent reviewer of a WRITE task, or whose accountable plan
reviewer is declared but reviews no WRITE task while an unrelated reviewer
handles the writes; an unreadable shared Work Registry) or `UNAVAILABLE` (a
non-`PROPOSE_WORK` packet; active Registry work overlapping the proposal; a
performer kind with no existing Team Execution adapter) instead of fabricating a
qualified manifest. Human, software and hybrid performers remain advisory: only
the AI performer kind has an existing Team Execution adapter, so any other kind
is `UNAVAILABLE` rather than a falsely launchable manifest. A role model
selection is configuration, not authority, and no provider is certified suitable
for private data by a draft.

Load-bearing properties:

- **Immutable and idempotent.** Drafts live under
  `$XDG_STATE_HOME/cao/intake-routes/`, directories `0700` and files `0600`,
  written through the same validated, atomic, fsynced, descriptor-relative store
  as admission. A draft id is a one-way digest of the immutable inputs, so an
  identical replay is idempotent, different evidence is a distinct draft, and a
  reused id for different content is a conflict — never a silent merge.
- **`--dry-run` creates no state.** It returns the would-be draft and writes no
  directory or file.
- **No dispatch, no model, no credential.** B4 opens no provider egress, runs no
  model, holds no credential and cannot start an agent.
- **Opaque text, screened for known credentials.** Objectives, work packages, the
  required capability, task prompts and role instructions are untrusted, opaque
  data: inspected and bounded, never executed or interpolated into a shell. Each
  is refused if it is credential-shaped under intake's known-credential screen
  (the same screen B1 admission uses) rather than persisted or silently redacted
  into different content. This is a fail-closed screen for **known** credential
  shapes only — it is not a guarantee that arbitrary secret text is detected — so
  every prompt still requires operator review, and the draft never carries the
  original submission body.

## Request-to-resolution status (B5): projection and mock gate

`cao intake status` is the B5 path. It derives **one truthful requester status**
and the accountable **next mover** for an already-admitted submission from
evidence that already exists -- the B1 admission, the B3 advisory packet, the B4
reviewable draft, the existing CAO team checkpoint, the shared Work Registry and
an optional authenticated acceptance source. `cao intake reply` is the B5
outbound gate and **simulates** one origin-channel followup, answer or event
receipt.

```sh
cao intake status --id <submission-id>
cao intake status --id <submission-id> --guest-view   # UNAVAILABLE in this build
cao intake reply --id <submission-id> --kind followup --auth-file auth.json --dry-run
cao intake reply --id <submission-id> --kind answer --auth-file auth.json --mock
```

It is **projection only**. B5 starts, merges, dispatches, schedules, claims,
sends and deploys nothing, writes no Registry row and writes no canonical
Company State. It keeps no new queue, no second ledger and no background
scheduler, and it **persists nothing at all**, so a repeated projection is
idempotent by construction.

Load-bearing properties:

- **Truthful, closed downward.** The status vocabulary is `ADMITTED`,
  `CLARIFICATION_REQUESTED`, `WORK_PROPOSED`, `IN_PROGRESS`, `WAITING_REVIEW`,
  `VERIFIED_ANSWER`, `COMPLETION`, `LATER_OUTCOME`, `DENIED_OR_REPLAN` and
  `UNKNOWN`. Derivation is deterministic and grounded in the evidence bundle: an
  `ASK_CONTEXT` advisory is *clarification requested*, a `PROPOSE_WORK` advisory
  without a draft is *work proposed*, a candidate answer, a `PROPOSED` draft and
  an active team are *waiting for review* or *in progress*.
- **No false closure.** `VERIFIED_ANSWER`, `COMPLETION` and `LATER_OUTCOME` are
  emitted **only** when an *authenticated* acceptance source positively reports
  the matching outcome. An advisory `ANSWER_CANDIDATE`, a `PROPOSED` draft, a
  `COMPLETE` team summary, an overlapping Registry claim, a green external check
  and any user-supplied status string are **never** acceptance. Where acceptance
  cannot be established the status stays `WAITING_REVIEW` (or `UNKNOWN`) and the
  named reviewer/operator is the next mover.
- **No acceptance connector in this build.** B5 ships no authenticated
  acceptance source, and no acceptance identity or connector exists, so this
  build never asserts closure: it reports the highest status the existing
  evidence honestly supports and names who moves next. A host that possesses an
  authenticated acceptance adapter injects it **in process**; there is
  deliberately no argv, submission-JSON or process-environment path to
  acceptance. The adapter is an in-process extension point, not a proof channel:
  a value that merely arrives through CLI JSON or the environment is untrusted
  and is never treated as authenticated acceptance.
- **External state is `UNKNOWN`.** External PR/review/acceptance state is not
  locally readable in this build, so it is reported `UNKNOWN` rather than
  guessed. B5 adds no GitHub client, no network call and no provider egress.
- **The guest view fails closed in this build.** `--guest-view` is
  `UNAVAILABLE`: a guest projection is only defensible for a caller a **host has
  independently authenticated**, and B5 ships no such host connector. Process
  environment and CLI arguments are caller-controlled, so a caller cannot assert
  a principal or a submission scope through `CAO_HOST_PRINCIPAL` /
  `CAO_HOST_PRINCIPAL_SUBMISSIONS`, or through any submission body; those values
  are untrusted input, never proof. The pure library projection
  (`guest_projection`) remains available for a host that *has* authenticated the
  caller, and it stays scope-bound and redacted: a submission outside the proven
  scope is `REFUSED`, and the projection carries no body, no item id, no
  evidence payload and no internal identity, writes no index and no second
  canonical state. Live guest access is not offered.
- **Outbound is gated and simulated.** B1/B2 source provides no authenticated
  delivery identity and no outbound credential, so an unqualified real send is a
  structured `UNAVAILABLE`. Only a deterministic `--dry-run` (egress `none`,
  `sent = false`) or an explicit `--mock` run of the **built-in** simulated
  adapter (`sent = false`, `simulation = true`) is served, and only after a
  separate **operator authorizing event** strictly bound to the submission, the
  resolution content and the **verified** originating recipient/context (the
  admitted submission's own source, never a value the authorization asserts).
  The mock pathway takes **no** caller-supplied adapter: it can only invoke the
  built-in no-egress simulation, so a caller cannot inject an object whose
  `send` reaches the network or has a real side effect while the call still
  reports `sent = false`. The send id is a deterministic idempotency key, so
  retries collapse; an ambiguous acknowledgement is `UNAVAILABLE` and is never
  rounded up to a send. There is no custom MCP server, HTTP service, email,
  Slack, Jira, ServiceNow API, secret or scheduler here.

## Governed execution handoff (B6): plan only, gated

`cao intake handoff` is the B6 path. It is the **bridge** the earlier phases
lack: it derives, from the *original* admission and its B3 advisory, one
independently reviewable **source-attested eligibility result**, and -- only
when that result qualifies an explicit, operator-qualified, scoped **READ**
execution -- one fail-closed, idempotent **handoff plan**.

```sh
cao intake handoff --id <submission-id> --dry-run     # derive a plan, no state
cao intake handoff --id <submission-id> --repo /path/repo
cao intake handoff --id <submission-id> --inspect     # read-only status
cao intake handoff --id <submission-id> --authorize   # UNAVAILABLE in this build
```

### Mention is not request

B3 flags a reserved word as a risk marker and hands almost every such request to
`HUMAN_REVIEW`, and B4 accepts only `PROPOSE_WORK`. B6 closes that gap with a new,
**independent** result. It classifies each reserved consequential-effect category
(`merge`, `deploy`, `release`, `publish`, `credential`, `permission`,
`destructive`) in the opaque request text as `REQUESTED`, `AMBIGUOUS`,
`MENTIONED`, `NEGATED` or `ABSENT`, using deterministic, bounded rules:

- an effect the request **explicitly asks for** is a `HOLD`;
- an effect the rules **cannot prove non-actionable** is a `HOLD` with a precise
  reason, never a silent pass;
- a **compound reference** ("squash-merge commit", "merge and deployment
  states"), a **conditional plan** ("if separately authorized", "when approved",
  "subject to review") and a **bare claim** ("the merge is complete") are
  mentions, not requests: they name an artifact, a future condition or a
  finished state;
- a request that mixes a consequential effect with a read scope is **never**
  silently downgraded to a READ plan (`mixed_effect_and_read_request`);
- only a request that declares a read scope and holds no consequential effect is
  `ELIGIBLE_READ`.

The result is derived, never asserted, and **the immutable B3 packet is never
mutated**: its fingerprint is bound, not rewritten. There is no LLM free-text
authority anywhere.

### What a handoff plan binds

A plan binds the submission id, the B3 packet fingerprint and disposition, the
source ref and HEAD (read-only Git evidence), the role/model/privacy
qualification, the immutable team/task ids (derived deterministically from the
submission id) and the plan's own content digest. The Registry owner is granted
by the operator authority and recorded on the plan's grant.

### PLAN_ONLY: the operator identity gate is missing

B6 starts, ticks, schedules, claims and deploys nothing. A plan becomes
executable **only** through an **in-process** authenticated operator-authority
adapter supplied by the host. A CLI flag, a process-environment value or a JSON
file is caller-controlled and is **never** proof, so this build ships no such
adapter and every otherwise-dispatchable plan is `PLAN_ONLY`. It reports the
exact missing capability and the smallest required integration:

> supply an in-process adapter through the library `operator_authority` argument
> that (1) authenticates the operator on a host-owned channel outside
> argv/env/JSON, (2) returns a grant bound to this plan's exact digest with
> `read_only = true`, and (3) names the operator, the scoped repository, the role,
> the model and the provider privacy qualification.

When every gate does prove -- authenticated operator, read-only grant bound to
this exact plan, in-scope repository, allowlisted model, provider privacy
qualified -- B6 hands **one** immutable `cao.team.v1` READ manifest to the
**existing** CAO team owner API (`team.validate_spec`, `team.initialize`,
`team.set_desired`). It never ticks: the existing R3/R4 daemon reconciles the
team. A held, merely-planned or refused handoff creates no team, no agent and no
Registry claim. `--authorize` is an honest structured `UNAVAILABLE`: the CLI
never constructs an authority and never accepts one.

### Already-started audits are linked, not duplicated

If an independent READ audit is already running for a submission, the plan is
`ALREADY_STARTED` and no second team is planned or launched. The link is
validated from authoritative CAO evidence: the team id must resolve, the
immutable manifest must be entirely READ, and the manifest itself must cite the
submission. Its status is read from the team checkpoint and its claims from the
ledger read-only, never from a caller-supplied `completed` field. Two competing
audits are an explicit refusal, never a collapsed link.

### Storage, idempotency and read-only status

Plans live under `$XDG_STATE_HOME/cao/intake-handoffs/` (`0700`/`0600`, through
the same validated, atomic, fsynced, descriptor-relative store as admission). The
plan id is a one-way digest of the immutable binding, so an identical replay is
idempotent (`DUPLICATE`), different evidence is a distinct plan, and a reused id
for different content is a conflict. `--dry-run` creates no state. `cao intake
handoff --inspect` and `cao intake status` derive a read-only handoff status from
authoritative evidence and persist nothing; `cao intake status` also reports the
validated linked team/claim snapshot.

### Still gated

Real operator authentication, email, Telegram, guest access and any
merge/deploy/permission action remain **gated and `UNAVAILABLE`**. Source state,
team completion and verified outcome stay distinct, and nothing here closes a
request. B6 adds no queue, no scheduler, no second ledger, no network call and no
credential.

## Durable Intake execution queue (B7): opt-in, daemon-reconciled

`cao intake queue` is the B7 path. B6 can derive one gated READ handoff plan,
but a plan that cannot be dispatched yet is not remembered. B7 persists bounded,
prequalified READ **intents** and reconciles them on the **existing** CAO
daemon's own tick into the **existing** CAO team owner API. It adds no
scheduler, no service, no database, no second ledger, no Registry, no
coordinator, no credential and no network egress.

The queue is **opt-in and default-disabled**: nothing is reconciled unless the
product configuration's `daemon.intake_queue` is explicitly `true`, and the
existing feedback/engineering watches keep their own independent defaults.

```sh
cao intake queue enqueue --id <submission-id> --repo /path/repo   # queue an intent
cao intake queue enqueue --item-id <item-id> --repo /path/repo --priority 10
cao intake queue enqueue --id <id> --repo /path/repo --dry-run   # validate only
cao intake queue list                                            # metadata only
cao intake queue show <intent-id>                                # one full record
```

The CLI **enqueues and reads only**. It never ticks the queue, never constructs
an operator authority and never starts a team: reconciliation belongs to the
existing daemon. An intent is queued only for a request that B6 derives as
`ELIGIBLE_READ` and for which an exact source ref/HEAD is readable; anything
else is reported truthfully and **nothing is persisted**.

### A queue intent is never its own approval

An intent records an immutable, source-bound **binding** and a mutable, derived
**decision receipt**; it never records authority. The only path that creates new
work is an **in-process** authenticated operator-authority adapter (the B6
contract). A CLI flag, a process-environment value or a JSON file is
caller-controlled and is never proof, and the daemon supplies no such adapter,
so a first dispatch is always held with the exact missing integration:

> supply an in-process adapter through the library `operator_authority` argument
> that (1) authenticates the operator on a host-owned channel outside
> argv/env/JSON, (2) returns a grant bound to this intent's exact plan digest
> with `read_only = true`, and (3) names the operator, the scoped repository, the
> role, the model and the provider privacy qualification.

### An already-approved team may re-verify and advance

A persisted, already-approved immutable CAO team bound to an intent may move
PAUSED -> RUNNING only after its approval/spec digest, its Intake binding, its
exact source HEAD and its dependencies are all re-validated from authoritative
evidence. The revalidation is delegated to the existing team owner API
(`team.set_desired`), which recomputes the confirmation; a forged manifest or
approval string is never trusted and the stored receipt is never consulted as
authority.

Any **preexisting** team -- PAUSED, RUNNING or COMPLETE -- is accepted as an
intent's own audit only when (1) its immutable manifest is entirely READ and
cites the submission, (2) **every** task targets the intent's **exact** bound
source repository, and (3) it carries a persisted operator admission whose spec
digest binds that exact manifest. An admission-less team (legacy approval mode,
or a manifest initialized outside the governed handoff) is held as
`team_unapproved` **before** the owner API is ever reached; a cross-source
manifest is held as `source_mismatch`; and an admission that no longer binds the
manifest is held as `approval_unverified`. A fake or foreign team, including one
labeled COMPLETE, therefore never grants progress or terminal dependency
acceptance: a COMPLETE label is terminal evidence only for the intent's exact
bound HEAD, and any other completed revision is held as `source_head_drift`. The
team manifest carries no per-task ref, so the exact ref/HEAD is re-verified from
the binding rather than from the manifest.

### Gates, isolation, fairness and a bounded budget

Every pass re-derives the B6 eligibility and binding and re-reads the exact
source HEAD, so a changed Intake binding, a moved HEAD, an unmet, missing or
cyclic dependency, an unreadable Work Registry or an independent overlapping
Registry claim **holds that item only**. A stored `COMPLETE` dependency status
and its content digest are caller-writable and are never authority on their own:
before it can release a dependent, the dependency's completion is re-derived
from its own authoritative team checkpoint -- the bound team must resolve and
report `COMPLETE`, its immutable manifest must be a fully READ, admission-bound
audit of the dependency submission with every task on the dependency's exact
bound source repository, every completed slot must sit at the dependency's exact
bound HEAD and its source ref/HEAD must still match -- and any missing,
unreadable or mismatched evidence holds the dependent as `dependency_unverified`.
These gates run for every preexisting status, not only the PAUSED advance, so a
RUNNING or COMPLETE label cannot bypass them. Every active Registry claim is
compared (none is skipped), so a conflicting claim beyond any fixed cut is never
missed. Items are ordered by
priority, then age, then id; the per-tick budget is checked before any side
effect (`team.set_desired` or the governed handoff dispatch), so a zero budget
starts nothing and reports `advanced = 0`, at most one item advances per tick,
and a held item never consumes that budget. Two concurrent passes are serialized
by a kernel lock and the loser reports `LOCKED` and mutates nothing. A corrupt
private record, a changed content digest or an unexpected store file fails the
whole pass closed.

### WRITE and every consequential effect stay held

Only a fully READ manifest is ever started. WRITE, new corrective batches,
PR/CI/merge/deploy, credentials, permission changes and canonical acceptance
are never dispatched here, and an intent carries no authority to elevate a
read-only triage. This build does **not** claim unattended portfolio
management: a first dispatch, a WRITE, a corrective batch, a merge, a deploy, a
credential or a permission change is held until a distinct, existing, qualified
scope-specific operator authority is verifiably present.

### Storage, idempotency and crash recovery

Intents live under `$XDG_STATE_HOME/cao/intake-queue/` (`0700` directory,
`0600` records), written through the existing CAO durable-state primitive with
a recomputed content digest. The enqueue read/limit/write section runs under the
same kernel pass lock as reconciliation, so a concurrent enqueue or pass can
neither race the bounded size nor overwrite a receipt it just derived; the loser
mutates nothing and reports `INTAKE_QUEUE_BUSY`. The intent id is a one-way
digest of the immutable binding, so a replay with the exact same priority and
dependencies is `DUPLICATE`, while a replay that keeps the binding but changes
the priority or dependencies is refused deterministically rather than reported
as identical, and the stored record and receipt are preserved. A receipt is
deterministic: identical evidence yields
the same receipt digest, so a repeated hold persists no new record. A crash
between the team effect and the receipt write is recovered on the next pass:
an already-running or paused team is re-observed from the team checkpoint, so no
duplicate team, execution or claim is ever created. A held, merely-queued or
refused intent creates no team, no agent and no Registry claim.

## Standing Intake authority and the autonomous pipeline (B8): opt-in, default-disabled

B8 is the first *safe end-to-end capability*: the existing daemon, when
explicitly enabled (`daemon.intake_pipeline = true`; the fresh-install default
is off), resolves a verified standing READ authority and runs bounded
deterministic discovery → B3 triage → B6 eligibility → B7 queue on one tick.
B4 stays draft-only and B5 stays a projection; a B3 disposition or a B4 draft is
never permission.

- **The authority is external, never caller-asserted.** An ``authenticated``
  bit, a CLI flag, an environment value or a user-owned JSON file is never
  proof. The only authority source is a single operator-provisioned, root-owned
  grant document in the fixed protected directory ``/etc/cao``
  (``intake-authority.json``), read descriptor-relative with ``O_NOFOLLOW`` on
  every component and owner/mode checks; CAO never creates or writes there. On
  a host with no provisioned grant every first dispatch is ``HOLD`` honestly.
- **Effective validity is mandatory.** The grant must carry a mandatory,
  future ``expires_at`` within a bounded TTL, must not be revoked and must not
  begin in the future; a revoked, expired or future grant is ``REFUSED`` before
  anything is discovered or queued.
- **Quota evidence is complete or nothing moves.** Consumption is re-derived
  from the existing durable work artifacts (bound READ teams and Registry
  claims) before every first dispatch, inspecting every team record; a missing,
  symlinked, corrupt or unreadable Registry is ``UNKNOWN``, and a truncated
  state is never re-interpreted as a fresh quota.
- **Policy and privacy stay checked.** The granted model must be explicitly
  listed in the configured model allowlist (a missing allowlist grants nothing)
  and the grant must be privacy-qualified; an unlisted or unqualified model is
  refused.
- **Discovery is fair and bounded.** The grant scope is honored before the
  per-tick bound, unrelated or held candidates can never silence authorized
  work behind a truncated prefix, and the daemon's durable tick count rotates
  the window so every authorized submission is eventually processed.

``cao intake authority`` reports the resolved standing authority read-only
(status only; it never dispatches). The in-process adapter lives only in the
library API and is never reachable from argv or the environment.

## Limitations and unresolved items

- **Marketplace policy is unresolved.** Whether and how a marketplace app may
  deliver a document to a local adapter is a platform policy question that is
  not settled here. CAO implements the receiving side only.
- **Credential integration is unresolved and out of scope.** CAO reads no
  marketplace credential, no platform token and no issue-tracker token. Any
  credential a connected adapter needs is the operator's, external to CAO, and
  never passed into intake.
- **CAO is not a service.** There is no listening socket, no inbound port and no
  background receiver. If nothing pipes a document to stdin, nothing is
  admitted.
- **One item, one record.** The admission itself is one item and one record;
  there is no scheduler and no retry daemon, and a retry is the sender's action
  and reconciles by id. The opt-in B7 execution queue (above) is a separate,
  default-disabled durable store that only remembers prequalified READ intents
  for the existing daemon to reconcile; it is not an inbox and never admits a
  submission.

## Tests

`tests/test_intake.py` (admission, idempotency, conflict, corruption,
concurrency, redaction, strict input, CLI, stored-record tamper detection,
private-path safety, list-absence handling, directory-ancestry durability,
post-validation parent-substitution refusal and the shared private-store
toolkit) and `tests/test_intake_bridge.py` (end-to-end stdin admission, retries
and reconciliation, collision, injection inertness, fail-closed
unsupported/deferred transports and transport-before-input ordering) are the
runnable check. `tests/test_intake_triage.py` is the B3 runnable check: all five
dispositions, refusal and unknown paths, stale sources, duplicate and
already-implemented detection, replay/conflict, private crash-safe storage,
prompt-injection inertness and the CLI. `tests/test_intake_compile.py` is the B4
runnable check: non-`PROPOSE_WORK` refusal, stale/revoked/unverifiable plan,
unqualified capability, missing review or authority, a plan reviewer declared
but unused by any WRITE task, wrong repository/path domain, active Registry
overlap, replay/duplicate/conflict, prompt-injection inertness,
credential-shaped task prompts and role instructions, symlinked/foreign route
input, `--dry-run` creating no state, zero agent/team/claim effects, denied
provider authority, an authority grant that is never asserted verified, and the
CLI.
`tests/test_intake_resolution.py` is the B5 runnable check: the full status
vocabulary; clarification requested, work proposed, in progress and waiting for
review; the authenticated-acceptance gate for verified answer, completion and
later outcomes; the refusal to close on a candidate answer, a proposed draft or
a `COMPLETE` team summary; fail-closed missing and invalid evidence; a guest
against another scope, a forged host environment and the fail-closed
`--guest-view`; wrong recipient; absent and stale authorization; retries,
ambiguous sends and failure of the built-in adapter; a caller-injected adapter
that can never execute; replay/idempotence; prompt-injection inertness; read-only
Registry and private `0700`/`0600` state; zero execution/claim/team effects; and
the CLI.
`tests/test_intake_handoff.py` is the B6
runnable check: the mention-vs-request classification; the mixed and ambiguous
hold; synthetic equivalents of four redacted requests; the authenticated-operator
gate against an unauthenticated, stale, write-scoped or out-of-scope caller and a
spoofed environment/CLI; the immutable B3 fingerprint; the source/HEAD binding
and drift; validated linking of an already-started READ audit and the refusal of
uncited, write or competing audits; duplicate admission and crash-restart durable
idempotency; a WRITE request that is never dispatched; no new agent or Registry
claim on a hold; the absence of any raw body, tenant text or credential in a
plan; private `0700`/`0600` state; and the CLI.
`tests/test_task_router.py` is the `cao request` entrypoint runnable check: the
finite BACKLOG/INVESTIGATE/IMPLEMENT decision, the deterministic text rules
(implement-now, later, review, mixed consequential effects, a negative directive
and the fail-closed B6 hold), the durable idempotent admission and duplicate
replay, the read-only governed-execution report and its shell-quoted commands,
the EXECUTING evidence rule (a desired state or an unstarted PENDING team is
`PROPOSED`, only a genuinely running task slot or a benign early idle is
`EXECUTING`), the manual owner-initiated `--execute` route (a chat hold with no
approval, a legacy advance through the owner path that dispatches no agent, an
existing approved team reconciled without a duplicate start, a repository or
identity gate) with zero worker or Registry effects, and the CLI and README
documentation.
`tests/test_boundaries.py` additionally
guards that intake never routes a durable record through the permissive state
reader and that triage, B4, B5 and B6 reach the Registry through read-only calls
only, open no egress surface and use no shell — and that B4 and B5 never reach a
team execution entry point, a product task, a Registry write, canonical state or
any private-store write, that B6 reaches the team owner API only inside its
gated dispatch and never ticks or schedules, that B5 `reply` accepts no
caller-supplied adapter, that B6's CLI never constructs or accepts an operator
authority, and that the CLI never serves a guest projection from
caller-controlled input. Every value is synthetic; nothing is shipped and no
private payload is used.
