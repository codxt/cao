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
- Directories are `0700` and files `0600`. `makedirs` only modes the leaf, so
  the CAO state root above the intake directory is hardened to `0700` as well.
- A record is written to a private temp file, fsynced, then hard-linked into
  place. `os.link` is atomic and fails closed on an existing path, so two
  concurrent submitters can neither overwrite each other nor duplicate a record.
- Reads and writes go through `cao.state`: a corrupt, malformed or
  unknown-schema record **raises** and is never rounded up to an empty result.

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
| `item_id` | yes | printable, space-free, <= 200 chars |
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

The transport is checked **before** the input is read, so a refused transport
consumes nothing and writes nothing.

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
concurrency, redaction, strict input, CLI) and `tests/test_intake_bridge.py`
(end-to-end stdin admission, retries and reconciliation, collision, injection
inertness, fail-closed unsupported/deferred transports) are the runnable check.
Every value is synthetic; nothing is shipped and no private payload is used.
