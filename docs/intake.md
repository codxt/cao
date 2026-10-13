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
  `0700` explicitly as well.
- A record is written to a private temp file, fsynced, then hard-linked into
  place and the containing directory fsynced **before** the admission is
  acknowledged. `os.link` is atomic and fails closed on an existing path, so
  two concurrent submitters can neither overwrite each other nor duplicate a
  record. A failure after the link is never acknowledged; an honest retry finds
  the record and reconciles it as a duplicate, and that duplicate path also
  syncs the directory (propagating a sync failure) so a link that is not yet
  durable is never acknowledged.
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
private-path safety, list-absence handling and fsync durability) and
`tests/test_intake_bridge.py` (end-to-end stdin admission, retries and
reconciliation, collision, injection inertness, fail-closed
unsupported/deferred transports and transport-before-input ordering) are the
runnable check. `tests/test_boundaries.py` additionally guards that intake never
routes a durable record through the permissive state reader. Every value is
synthetic; nothing is shipped and no private payload is used.
