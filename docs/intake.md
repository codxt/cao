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
- **One item, one record.** There is no queue, no scheduler and no retry daemon;
  a retry is the sender's action, and it reconciles by id.

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
`tests/test_boundaries.py` additionally
guards that intake never routes a durable record through the permissive state
reader and that triage and B4 reach the Registry through read-only calls only,
open no egress surface and use no shell — and that B4 never reaches a team
execution entry point, a product task, a Registry write or canonical state.
Every value is synthetic; nothing is shipped and no private payload is used.
